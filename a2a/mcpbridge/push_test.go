package mcpbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	testClusterName    = "prod-us-central1"
	testKubeContext    = "gke_example-project_us-central1_prod-us-central1"
	testUnreachableCtx = "gke_example-project_us-central1_unreachable"
	testNamespace      = "kubeagents-system"
	testPAName         = "platform-agent"
	testProjectID      = "example-project"
	testLocation       = "us-central1"
	testGitHubRepo     = "example-org/example-infra"
	testConsoleAuthVal = "console-secret-token"
	testGatewayAuthVal = "gateway-secret-token"
	testPRNumber       = 42
	testPRURL          = "https://github.com/example-org/example-infra/pull/42"
	testAGYConvID      = "agy-conv-uuid-1234"
	testBgTaskID       = "task-bg-99"
)

func startMockGatewayMCPServer(t *testing.T, pendingEvents *[]PushEvent) *httptest.Server {
	t.Helper()
	var mu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case remoteMCPHealthPath:
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"ok","name":"kube-agents","version":"0.4.0"}`))
		case remoteMCPPath:
			var req rpcRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			var callParams struct {
				Name      string          `json:"name"`
				Arguments json.RawMessage `json:"arguments"`
				Meta      struct {
					ProgressToken any `json:"progressToken"`
				} `json:"_meta"`
			}
			_ = json.Unmarshal(req.Params, &callParams)
			if strings.Contains(r.Header.Get("Accept"), contentTypeNDJSON) {
				w.Header().Set("Content-Type", contentTypeNDJSON)
				w.WriteHeader(http.StatusOK)
				if callParams.Name == toolSendMessage && callParams.Meta.ProgressToken != nil {
					progFrame, _ := json.Marshal(map[string]any{
						"jsonrpc": jsonRPCVersion,
						"method":  methodProgressNotification,
						"params": map[string]any{
							"progressToken": callParams.Meta.ProgressToken,
							"progress":      1,
							"message":       "Step 1: Checking GKE cluster nodes",
						},
					})
					_, _ = fmt.Fprintf(w, "%s\n", progFrame)
				}
				resp := marshalRPCResult(req.ID, CallToolResult{
					Content: []TextContent{{
						Type: contentTypeText,
						Text: fmt.Sprintf("Gateway executed `%s` successfully.", callParams.Name),
					}},
				})
				_, _ = fmt.Fprintf(w, "%s\n", resp)
				return
			}
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(marshalRPCResult(req.ID, CallToolResult{
				Content: []TextContent{{
					Type: contentTypeText,
					Text: fmt.Sprintf("Gateway executed `%s` successfully.", callParams.Name),
				}},
			}))
		case remoteMCPSwarmPath:
			var swarmReq RemoteSwarmRequest
			if err := json.NewDecoder(r.Body).Decode(&swarmReq); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			snap, nextLedger := buildLocalSwarmState(SwarmProjectionInput{
				RootConversationID: swarmReq.RootConversationID,
				Deployment:         swarmReq.Deployment,
				Connected:          true,
				Conversations:      swarmReq.Conversations,
				Subagents:          swarmReq.Subagents,
				Now:                time.Now().UTC(),
			}, swarmReq.Ledger)
			var drained []PushEvent
			if pendingEvents != nil {
				mu.Lock()
				drained = append(drained, (*pendingEvents)...)
				*pendingEvents = nil
				mu.Unlock()
			}
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(RemoteSwarmResponse{
				Connected:          true,
				Deployment:         swarmReq.Deployment,
				Snapshot:           snap,
				Ledger:             nextLedger,
				Conversations:      swarmReq.Conversations,
				ActiveConversation: swarmReq.ActiveConversation,
				PendingEvents:      drained,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestBiDirectionalPushPiggybackAndAgentAPIWakeup(t *testing.T) {
	var remoteQueue []PushEvent
	gwSrv := startMockGatewayMCPServer(t, &remoteQueue)
	ctx := context.Background()

	tmpDir := t.TempDir()
	fakeAgentAPI := filepath.Join(tmpDir, "fake-agentapi")
	if err := os.WriteFile(fakeAgentAPI, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var agentAPICalls [][]string
	mockRunner := func(_ context.Context, name string, args ...string) ([]byte, error) {
		if name == fakeAgentAPI {
			mu.Lock()
			agentAPICalls = append(agentAPICalls, append([]string{name}, args...))
			mu.Unlock()
			return []byte("ok"), nil
		}
		return nil, fmt.Errorf("unexpected runner call: %s", name)
	}

	store, err := NewConfigStore(filepath.Join(tmpDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store, Runner: mockRunner})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	prof := DeploymentProfile{
		Name:              testClusterName,
		Namespace:         testNamespace,
		PlatformAgentName: testPAName,
		DirectNATSURL:     gwSrv.URL,
		BusAuthValue:      testConsoleAuthVal,
		ObserverAuthValue: testGatewayAuthVal,
		Ready:             true,
	}
	_ = store.UpsertDeployments([]DeploymentProfile{prof})
	if _, err := mcpSrv.connectSession(ctx, prof); err != nil {
		t.Fatalf("connectSession: %v", err)
	}

	// Part A: With reactive push disabled, queue a task completion event and verify piggybacking on next tool call.
	disabled := false
	_, _ = store.UpdatePushConfig("", &disabled, fakeAgentAPI)
	_ = store.AppendPendingEvents(PushEvent{
		ID:        "env-bg-3",
		Kind:      eventKindTaskCompleted,
		TaskID:    testBgTaskID,
		Title:     "Hourly check",
		Summary:   "All 12 nodes healthy",
		Timestamp: time.Now().UTC(),
	})

	statusRes := mcpSrv.CallTool(ctx, toolConnectionStatus, nil)
	if !strings.Contains(statusRes.Content[0].Text, "Pending Kube-Agents Push Events") ||
		!strings.Contains(statusRes.Content[0].Text, "All 12 nodes healthy") {
		t.Fatalf("expected piggybacked push event in connection_status response, got:\n%s", statusRes.Content[0].Text)
	}

	// Part B: Arm reactive push with AGYConversationID and enqueue a proactive notification via PushDispatcher.
	enabled := true
	_, _ = store.UpdatePushConfig(testAGYConvID, &enabled, fakeAgentAPI)
	mcpSrv.dispatcher.Enqueue(PushEvent{
		ID:        "notify-1",
		Kind:      eventKindProactiveNotify,
		Title:     "Kanban Board Updated",
		Summary:   "Card #18 moved to Done.",
		Timestamp: time.Now().UTC(),
	})

	deadline := time.Now().Add(4 * time.Second)
	for {
		mu.Lock()
		callCount := len(agentAPICalls)
		mu.Unlock()
		if callCount > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("timed out waiting for PushDispatcher to invoke agentapi send-message")
		}
		time.Sleep(50 * time.Millisecond)
	}

	mu.Lock()
	call := agentAPICalls[0]
	mu.Unlock()
	joinedCall := strings.Join(call, " ")
	if !strings.Contains(joinedCall, testAGYConvID) || !strings.Contains(joinedCall, "Kanban Board Updated") {
		t.Errorf("unexpected agentapi invocation: %v", call)
	}

	// Part C: Test HandleAGYHook ignores unrelated conversations and injects pending events only for known kube-agents conversations.
	_ = store.AppendPendingEvents(PushEvent{
		ID:        "hook-ev-1",
		Kind:      eventKindNoticePR,
		Title:     "PR #77 Opened",
		Summary:   "Automated fix PR",
		PRLinks:   []string{"https://github.com/example-org/example-infra/pull/77"},
		Timestamp: time.Now().UTC(),
	})
	var unrelatedOut bytes.Buffer
	if err := HandleAGYHook(store, strings.NewReader(`{"conversationId":"unrelated-coding-conv-99"}`), &unrelatedOut); err != nil {
		t.Fatalf("HandleAGYHook (unrelated): %v", err)
	}
	if store.ResolveAGYConversationID() != testAGYConvID {
		t.Errorf("ResolveAGYConversationID after unrelated hook = %q, want %q", store.ResolveAGYConversationID(), testAGYConvID)
	}
	if strings.TrimSpace(unrelatedOut.String()) != "{}" {
		t.Errorf("expected unrelated hook to not drain events, got: %s", unrelatedOut.String())
	}

	var hookOut bytes.Buffer
	if err := HandleAGYHook(store, strings.NewReader(fmt.Sprintf(`{"conversationId":%q}`, testAGYConvID)), &hookOut); err != nil {
		t.Fatalf("HandleAGYHook: %v", err)
	}
	if !strings.Contains(hookOut.String(), "ephemeralMessage") || !strings.Contains(hookOut.String(), "PR #77 Opened") {
		t.Errorf("unexpected hook output: %s", hookOut.String())
	}

	// Part D: Verify NDJSON progress streaming from remote gateway through local stdio bridge.
	var progressSteps []string
	progCtx := WithProgressCallback(ctx, func(_ int, message string) {
		progressSteps = append(progressSteps, message)
	})
	sendArgs, _ := json.Marshal(map[string]any{"message": "Check cluster health"})
	sendRes := mcpSrv.CallTool(progCtx, toolSendMessage, sendArgs)
	if sendRes.IsError {
		t.Fatalf("send_message via gateway bridge failed: %s", sendRes.Content[0].Text)
	}
	if len(progressSteps) == 0 || !strings.Contains(progressSteps[0], "Checking GKE cluster nodes") {
		t.Fatalf("expected live NDJSON progress notification from remote gateway, got %v", progressSteps)
	}
}

func TestDefaultOverviewConnectedAndDisconnectedStates(t *testing.T) {
	ctx := context.Background()
	tmpDir := t.TempDir()
	cfgPath := filepath.Join(tmpDir, "mcp-config.json")
	brainDir := filepath.Join(tmpDir, "brain")
	store, err := NewConfigStore(cfgPath)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}

	readDefaultView := func() SwarmView {
		t.Helper()
		viewPath := filepath.Join(brainDir, defaultDisconnectedSwarmID, systemGeneratedDirName, swarmDirName, swarmViewJSONFileName)
		raw, rErr := os.ReadFile(viewPath)
		if rErr != nil {
			t.Fatalf("reading default view.json: %v", rErr)
		}
		var v SwarmView
		if uErr := json.Unmarshal(raw, &v); uErr != nil {
			t.Fatalf("unmarshaling default view.json: %v", uErr)
		}
		return v
	}

	// 1. Before any deployment is connected, _kube_agents_default renders Disconnected Overview.
	SyncDefaultDisconnectedOverview(ctx, brainDir, store)
	v1 := readDefaultView()
	if v1.KubeAgents == nil || v1.KubeAgents.Connected {
		t.Fatalf("expected Disconnected Overview before connecting, got %+v", v1.KubeAgents)
	}

	// 2. When a deployment is active and connected, _kube_agents_default renders Connected Overview.
	_ = store.UpsertDeployments([]DeploymentProfile{
		{
			Name:              "prod-cluster",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform-agent",
			ClusterName:       "prod-cluster",
			Version:           "0.16.0",
			LatestReleaseTag:  "0.16.0",
			Ready:             true,
		},
	})
	if _, sErr := store.SetActiveDeployment("prod-cluster"); sErr != nil {
		t.Fatalf("SetActiveDeployment: %v", sErr)
	}
	if !store.IsDefaultOverviewConnected("") {
		t.Fatalf("expected IsDefaultOverviewConnected to be true after SetActiveDeployment")
	}
	SyncDefaultDisconnectedOverview(ctx, brainDir, store)
	v2 := readDefaultView()
	if v2.KubeAgents == nil || !v2.KubeAgents.Connected {
		t.Fatalf("expected Connected Overview when deployment is connected, got %+v", v2.KubeAgents)
	}

	// 3. When disconnected via SetConversationDisconnected, _kube_agents_default renders Disconnected Overview.
	store.SetConversationDisconnected("", true)
	if store.IsDefaultOverviewConnected("") {
		t.Fatalf("expected IsDefaultOverviewConnected to be false after SetConversationDisconnected(true)")
	}
	SyncDefaultDisconnectedOverview(ctx, brainDir, store)
	v3 := readDefaultView()
	if v3.KubeAgents == nil || v3.KubeAgents.Connected {
		t.Fatalf("expected Disconnected Overview after SetConversationDisconnected(true), got %+v", v3.KubeAgents)
	}
}
