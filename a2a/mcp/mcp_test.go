package mcp

import (
	"bytes"
	"context"
	"encoding/base64"
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

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const (
	testHostLoopback      = "127.0.0.1"
	testReadyTimeout      = 15 * time.Second
	testClusterName       = "kube-agents-host"
	testKubeContext       = "gke_example-project_us-east4_kube-agents-host"
	testUnreachableCtx    = "gke_unreachable_context"
	testNamespace         = "kubeagents-system"
	testPAName            = "platform-agent"
	testProjectID         = "example-project"
	testLocation          = "us-east4"
	testGitHubRepo        = "example-org/example-infra"
	testConsoleAuthVal    = "console-pass-val"
	testGatewayAuthVal    = "gateway-pass-val"
	testJetskiConvID      = "00000000-0000-0000-0000-000000000001"
	testPRNumber          = 42
	testPRURL             = "https://github.com/example-org/example-infra/pull/42"
	testParentTaskID      = "t-parent-101"
	testChildTaskID       = "t-child-102"
	testWakeTaskID        = "t-wake-103"
	testBgTaskID          = "t-bg-999"
	testContextID         = "c-ctx-100"
	testSessionAddressee  = "s-worker01"
	testPlatformAddressee = "platform"
)

func startEmbeddedJetStream(t *testing.T) (*natsserver.Server, *nats.Conn, jetstream.JetStream) {
	t.Helper()
	opts := &natsserver.Options{
		Host:      testHostLoopback,
		Port:      -1,
		NoLog:     true,
		NoSigs:    true,
		JetStream: true,
		StoreDir:  t.TempDir(),
	}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("natsserver.NewServer: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(testReadyTimeout) {
		t.Fatal("embedded nats-server did not become ready")
	}
	t.Cleanup(srv.Shutdown)

	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("nats.Connect: %v", err)
	}
	t.Cleanup(nc.Close)

	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream.New: %v", err)
	}

	ctx := context.Background()
	streamDefs := []jetstream.StreamConfig{
		{Name: streamTasks, Subjects: []string{"a2a.tasks.*.*.in", "a2a.tasks.*.*.out"}},
		{Name: streamDirectory, Subjects: []string{"a2a.agents.*.card"}},
		{Name: streamTopicsState, Subjects: []string{"a2a.topics.state.>"}},
		{Name: streamTopicsJournal, Subjects: []string{"a2a.topics.journal.>"}},
	}
	for _, sc := range streamDefs {
		if _, err := js.CreateStream(ctx, sc); err != nil {
			t.Fatalf("CreateStream(%s): %v", sc.Name, err)
		}
	}
	return srv, nc, js
}

func TestConfigStorePersistenceAndConversations(t *testing.T) {
	cfgFile := filepath.Join(t.TempDir(), "mcp-config.json")
	store, err := NewConfigStore(cfgFile)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}

	err = store.UpsertDeployments([]DeploymentProfile{
		{
			Name:              testClusterName,
			KubeContext:       testKubeContext,
			Namespace:         testNamespace,
			PlatformAgentName: testPAName,
			ClusterName:       testClusterName,
			GitHubRepo:        testGitHubRepo,
			Ready:             true,
		},
	})
	if err != nil {
		t.Fatalf("UpsertDeployments: %v", err)
	}

	active, ok := store.ActiveDeployment()
	if !ok || active.Name != testClusterName {
		t.Fatalf("ActiveDeployment = %+v, ok=%v; want %s", active, ok, testClusterName)
	}

	conv, err := store.EnsureConversation("incident-42", true, "Investigating node upgrade")
	if err != nil {
		t.Fatalf("EnsureConversation: %v", err)
	}
	if conv.Alias != "incident-42" || !strings.HasPrefix(conv.Token, defaultConvTokenPrefix) {
		t.Fatalf("unexpected conversation entry: %+v", conv)
	}
	if _, err := store.SetConversationSessionMode("incident-42", true); err != nil {
		t.Fatalf("SetConversationSessionMode: %v", err)
	}

	reloaded, err := NewConfigStore(cfgFile)
	if err != nil {
		t.Fatalf("reloading ConfigStore: %v", err)
	}
	if reloaded.ActiveConversation().Alias != "incident-42" || !reloaded.ActiveConversation().SessionMode {
		t.Fatalf("reloaded conversation state mismatch: %+v", reloaded.ActiveConversation())
	}

	ev := PushEvent{
		ID:        "ev-1",
		Kind:      eventKindProactiveNotify,
		Title:     "Upgrade Check",
		Summary:   "Cluster is ready for 1.32",
		Timestamp: time.Now().UTC(),
	}
	if err := reloaded.AppendPendingEvents(ev); err != nil {
		t.Fatalf("AppendPendingEvents: %v", err)
	}
	if err := reloaded.AppendPendingEvents(ev); err != nil {
		t.Fatalf("duplicate AppendPendingEvents: %v", err)
	}
	peeked := reloaded.PeekPendingEvents()
	if len(peeked) != 1 {
		t.Fatalf("PeekPendingEvents len = %d, want 1", len(peeked))
	}
	drained, err := reloaded.DrainPendingEvents()
	if err != nil || len(drained) != 1 {
		t.Fatalf("DrainPendingEvents = %+v, err=%v", drained, err)
	}
	if after := reloaded.PeekPendingEvents(); len(after) != 0 {
		t.Fatalf("expected 0 events after drain, got %d", len(after))
	}
}

func TestDiscoverDeploymentsAndCredentials(t *testing.T) {
	mockRunner := func(_ context.Context, _ string, args ...string) ([]byte, error) {
		joined := strings.Join(args, " ")
		switch {
		case strings.Contains(joined, "config get-contexts"):
			return []byte(testKubeContext + "\n" + testUnreachableCtx + "\n"), nil
		case strings.Contains(joined, testUnreachableCtx):
			return nil, fmt.Errorf("context deadline exceeded")
		case strings.Contains(joined, k8sResourcePlatformAgent):
			return json.Marshal(map[string]any{
				"items": []map[string]any{
					{
						"metadata": map[string]any{
							"name":      testPAName,
							"namespace": testNamespace,
						},
						"spec": map[string]any{
							"mode":    "next",
							"version": "0fe1c8ce",
							"harness": map[string]any{
								"projectId":   testProjectID,
								"clusterName": testClusterName,
								"location":    testLocation,
							},
							"integration": map[string]any{
								"github": map[string]any{
									"gitRepo": testGitHubRepo,
								},
								"gchat": map[string]any{"enabled": true},
							},
						},
						"status": map[string]any{
							"conditions": []map[string]any{
								{"type": "Ready", "status": "True", "message": "Reconciled"},
							},
						},
					},
				},
			})
		case strings.Contains(joined, k8sResourceAgentProfile):
			return json.Marshal(map[string]any{
				"items": []map[string]any{
					{"metadata": map[string]any{"name": "default", "namespace": testNamespace}},
				},
			})
		case strings.Contains(joined, k8sResourceSecret):
			return json.Marshal(map[string]any{
				"data": map[string]string{
					consoleAuthField: base64.StdEncoding.EncodeToString([]byte(testConsoleAuthVal + "\n")),
					gatewayAuthField: base64.StdEncoding.EncodeToString([]byte(testGatewayAuthVal + "\n")),
				},
			})
		default:
			return nil, fmt.Errorf("unexpected command: %s", joined)
		}
	}

	profiles, err := DiscoverDeployments(context.Background(), mockRunner, "", true)
	if err != nil {
		t.Fatalf("DiscoverDeployments: %v", err)
	}
	if len(profiles) != 1 {
		t.Fatalf("got %d profiles, want 1", len(profiles))
	}
	p := profiles[0]
	if p.Name != testClusterName || !p.Ready || p.GitHubRepo != testGitHubRepo {
		t.Errorf("unexpected profile metadata: %+v", p)
	}
	if p.BusAuthValue != testConsoleAuthVal || p.ObserverAuthValue != testGatewayAuthVal {
		t.Errorf("credentials not decoded properly: bus=%q observer=%q", p.BusAuthValue, p.ObserverAuthValue)
	}
}

func TestBusSessionAndMCPServerEndToEnd(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
	ctx := context.Background()

	seedBusTopicsAndCard(t, nc)
	startSimulatedGatewayAndWorkers(t, nc)

	cfgPath := filepath.Join(t.TempDir(), "mcp-config.json")
	store, err := NewConfigStore(cfgPath)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	t.Cleanup(mcpSrv.Close)

	// 1. Test initialize over JSON-RPC.
	initResp := mcpSrv.HandleJSONRPC(ctx, []byte(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}`))
	if !strings.Contains(string(initResp), `"name":"kube-agents"`) {
		t.Fatalf("unexpected initialize response: %s", initResp)
	}

	// 2. Test tools/list.
	listResp := mcpSrv.HandleJSONRPC(ctx, []byte(`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`))
	for _, requiredTool := range []string{
		toolDiscoverDeployments,
		toolConnectDeployment,
		toolConnectionStatus,
		toolSendMessage,
		toolManageSession,
		toolTaskControl,
		toolReadTopics,
		toolInspectPR,
		toolPollEvents,
	} {
		if !strings.Contains(string(listResp), requiredTool) {
			t.Errorf("tools/list missing %q", requiredTool)
		}
	}

	// 3. Connect to embedded NATS deployment.
	connectArgs, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv.ClientURL(),
	})
	connRes := mcpSrv.CallTool(ctx, toolConnectDeployment, connectArgs)
	if connRes.IsError || !strings.Contains(connRes.Content[0].Text, "TASKS") {
		t.Fatalf("connect_deployment failed: %+v", connRes)
	}

	// 4. Read blackboard topics.
	topicsRes := mcpSrv.CallTool(ctx, toolReadTopics, json.RawMessage(`{"action":"read","topic":"upgrade-readiness"}`))
	if topicsRes.IsError || !strings.Contains(topicsRes.Content[0].Text, "Cluster is ready for 1.32") {
		t.Fatalf("read_topics failed: %+v", topicsRes)
	}

	// 5. Send message in session mode triggering thinking -> activity -> delegate -> child -> wake -> chat/v1 result!
	// By default (debug == false), thinking is rendered collapsed (<details><summary>Thought...) and tool call tables/timings are hidden.
	sendArgs, _ := json.Marshal(map[string]any{
		"message":              "Check upgrade readiness and open a PR if needed",
		"mode":                 modeSession,
		"wait_timeout_seconds": 15,
	})
	sendRes := mcpSrv.CallTool(ctx, toolSendMessage, sendArgs)
	if sendRes.IsError {
		t.Fatalf("send_message returned error: %s", sendRes.Content[0].Text)
	}
	outText := sendRes.Content[0].Text
	for _, expectedSnippet := range []string{
		"<details>\n<summary>Thought",
		"Analyzing cluster upgrade readiness",
		"Upgrade Readiness Confirmed & PR Opened",
		testPRURL,
	} {
		if !strings.Contains(outText, expectedSnippet) {
			t.Errorf("send_message default output missing %q:\n%s", expectedSnippet, outText)
		}
	}
	for _, hiddenByDefault := range []string{
		"<details open>",
		"Tool Activity Stream",
		"run_kubectl",
		"flowchart LR",
	} {
		if strings.Contains(outText, hiddenByDefault) {
			t.Errorf("send_message default output should NOT contain %q when debug=false:\n%s", hiddenByDefault, outText)
		}
	}

	// 6. Verify task_control 'get' with debug=true shows the full chain, tool calls, timings, and task IDs.
	tcRes := mcpSrv.CallTool(ctx, toolTaskControl, json.RawMessage(`{"action":"get","task_id":"`+testParentTaskID+`","debug":true}`))
	if tcRes.IsError {
		t.Fatalf("task_control get failed: %s", tcRes.Content[0].Text)
	}
	debugText := tcRes.Content[0].Text
	for _, expectedDebugSnippet := range []string{
		testWakeTaskID,
		"Tool Activity Stream",
		"run_kubectl",
		"85ms",
		"Delegated Child Task #1",
		"Session Wake Synthesis Task",
	} {
		if !strings.Contains(debugText, expectedDebugSnippet) {
			t.Errorf("task_control debug=true output missing %q:\n%s", expectedDebugSnippet, debugText)
		}
	}
	// Also test manage_session debug_on and debug_off toggles.
	msDebugOn := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"debug_on"}`))
	if msDebugOn.IsError || !store.DebugMode() {
		t.Fatalf("manage_session debug_on failed: %+v", msDebugOn)
	}
	msDebugOff := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"debug_off"}`))
	if msDebugOff.IsError || store.DebugMode() {
		t.Fatalf("manage_session debug_off failed: %+v", msDebugOff)
	}

	// 7. Verify manage_session list, new, switch, transcript, and task_control list.
	msNew := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"new","alias":"triage-2","description":"Second session"}`))
	if msNew.IsError || !strings.Contains(msNew.Content[0].Text, "triage-2") {
		t.Fatalf("manage_session new failed: %+v", msNew)
	}
	msList := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"list"}`))
	if msList.IsError || !strings.Contains(msList.Content[0].Text, "triage-2") {
		t.Fatalf("manage_session list failed: %+v", msList)
	}
	msSwitch := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"switch","alias":"default"}`))
	if msSwitch.IsError || !strings.Contains(msSwitch.Content[0].Text, "default") {
		t.Fatalf("manage_session switch failed: %+v", msSwitch)
	}
	msTranscript := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"transcript","alias":"default"}`))
	if msTranscript.IsError || !strings.Contains(msTranscript.Content[0].Text, testParentTaskID) {
		t.Fatalf("manage_session transcript failed: %+v", msTranscript)
	}
	tcList := mcpSrv.CallTool(ctx, toolTaskControl, json.RawMessage(`{"action":"list"}`))
	if tcList.IsError || !strings.Contains(tcList.Content[0].Text, testParentTaskID) {
		t.Fatalf("task_control list failed: %+v", tcList)
	}

	// 8. Verify ServeStdio with resources/list, resources/read, ping, notifications/initialized.
	stdioIn := strings.Join([]string{
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":10,"method":"ping"}`,
		`{"jsonrpc":"2.0","id":11,"method":"resources/list"}`,
		`{"jsonrpc":"2.0","id":12,"method":"resources/read","params":{"uri":"kube-agents://status"}}`,
		`{"jsonrpc":"2.0","id":13,"method":"resources/read","params":{"uri":"kube-agents://topics"}}`,
		`{"jsonrpc":"2.0","id":14,"method":"resources/read","params":{"uri":"kube-agents://tasks"}}`,
		`{"jsonrpc":"2.0","id":15,"method":"resources/read","params":{"uri":"kube-agents://events"}}`,
		`{"jsonrpc":"2.0","id":16,"method":"prompts/list"}`,
	}, "\n") + "\n"
	var stdioOut bytes.Buffer
	if err := mcpSrv.ServeStdio(ctx, strings.NewReader(stdioIn), &stdioOut); err != nil {
		t.Fatalf("ServeStdio: %v", err)
	}
	outLines := strings.Split(strings.TrimSpace(stdioOut.String()), "\n")
	if len(outLines) != 7 {
		t.Fatalf("expected 7 JSON-RPC responses (1 notification ignored), got %d:\n%s", len(outLines), stdioOut.String())
	}
}

func TestBiDirectionalPushPiggybackAndAgentAPIWakeup(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
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

	// Connect with ObserverAuthValue set so the passive chat.notify.* tap is active.
	prof := DeploymentProfile{
		Name:              testClusterName,
		Namespace:         testNamespace,
		PlatformAgentName: testPAName,
		DirectNATSURL:     srv.ClientURL(),
		ObserverAuthValue: "obs-token",
		Ready:             true,
	}
	_ = store.UpsertDeployments([]DeploymentProfile{prof})
	if _, err := mcpSrv.connectSession(ctx, prof); err != nil {
		t.Fatalf("connectSession: %v", err)
	}

	// Part A: With reactive push disabled, publish an asynchronous task completion and verify piggybacking on next tool call.
	disabled := false
	_, _ = store.UpdatePushConfig("", &disabled, fakeAgentAPI)

	publishTaskEnv(t, nc, "a2a.tasks.platform."+testBgTaskID+".in", lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-bg-1",
		TS:         time.Now().UTC(),
		TaskID:     testBgTaskID,
		ContextID:  testContextID,
		Kind:       lib.KindMessage,
		From:       lib.Party{Session: "cron"},
		Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-bg-1", Parts: []lib.Part{{Kind: "text", Text: "Hourly check"}}}),
	})
	publishTaskEnv(t, nc, "a2a.tasks.platform."+testBgTaskID+".out", lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-bg-2",
		TS:         time.Now().UTC(),
		TaskID:     testBgTaskID,
		ContextID:  testContextID,
		Kind:       lib.KindArtifactUpdate,
		From:       lib.Party{Session: "platform"},
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:    testBgTaskID,
			ContextID: testContextID,
			Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "All 12 nodes healthy"}}},
		}),
	})
	publishTaskEnv(t, nc, "a2a.tasks.platform."+testBgTaskID+".out", lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "env-bg-3",
		TS:         time.Now().UTC(),
		TaskID:     testBgTaskID,
		ContextID:  testContextID,
		Kind:       lib.KindStatusUpdate,
		From:       lib.Party{Session: "platform"},
		Payload: mustJSON(lib.StatusUpdate{
			TaskID:    testBgTaskID,
			ContextID: testContextID,
			Status:    lib.TaskStatus{State: lib.StateCompleted},
			Final:     true,
		}),
	})
	_ = nc.Flush()

	deadline := time.Now().Add(3 * time.Second)
	for len(store.PeekPendingEvents()) == 0 && time.Now().Before(deadline) {
		time.Sleep(25 * time.Millisecond)
	}
	if len(store.PeekPendingEvents()) == 0 {
		t.Fatal("expected background task completion to be queued in pending events")
	}

	statusRes := mcpSrv.CallTool(ctx, toolConnectionStatus, nil)
	if !strings.Contains(statusRes.Content[0].Text, "Pending Kube-Agents Push Events") ||
		!strings.Contains(statusRes.Content[0].Text, "All 12 nodes healthy") {
		t.Fatalf("expected piggybacked push event in connection_status response, got:\n%s", statusRes.Content[0].Text)
	}

	// Part B: Arm reactive push with JetskiConversationID and publish a proactive chat.notify.gchat message.
	enabled := true
	_, _ = store.UpdatePushConfig(testJetskiConvID, &enabled, fakeAgentAPI)

	notifyPayload := mustJSON(lib.NotifyRequest{
		Text: "Kanban card moved to Done: Upgrade GKE node pool",
		Chat: mustJSON(chatmsg.Message{
			Schema:   chatmsg.Schema,
			Headline: "Kanban Board Updated",
			Tone:     chatmsg.ToneSuccess,
			Detail:   "Card #18 moved to Done.",
		}),
	})
	if err := nc.Publish("chat.notify.gchat", notifyPayload); err != nil {
		t.Fatal(err)
	}
	_ = nc.Flush()

	deadline = time.Now().Add(4 * time.Second)
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
	if !strings.Contains(joinedCall, testJetskiConvID) || !strings.Contains(joinedCall, "Kanban Board Updated") {
		t.Errorf("unexpected agentapi invocation: %v", call)
	}

	// Part C: Test HandleJetskiHook binding conversationId and injecting pending events.
	_ = store.AppendPendingEvents(PushEvent{
		ID:        "hook-ev-1",
		Kind:      eventKindNoticePR,
		Title:     "PR #77 Opened",
		Summary:   "Automated fix PR",
		PRLinks:   []string{"https://github.com/example-org/example-infra/pull/77"},
		Timestamp: time.Now().UTC(),
	})
	var hookOut bytes.Buffer
	if err := HandleJetskiHook(store, strings.NewReader(`{"conversationId":"conv-from-hook-99"}`), &hookOut); err != nil {
		t.Fatalf("HandleJetskiHook: %v", err)
	}
	if store.ResolveJetskiConversationID() != "conv-from-hook-99" {
		t.Errorf("ResolveJetskiConversationID = %q, want conv-from-hook-99", store.ResolveJetskiConversationID())
	}
	if !strings.Contains(hookOut.String(), "ephemeralMessage") || !strings.Contains(hookOut.String(), "PR #77 Opened") {
		t.Errorf("unexpected hook output: %s", hookOut.String())
	}
}

func TestInspectPullRequestNativeDiffFiles(t *testing.T) {
	const sampleDiff = `diff --git a/clusters/prod/nodepool.yaml b/clusters/prod/nodepool.yaml
index 1111111..2222222 100644
--- a/clusters/prod/nodepool.yaml
+++ b/clusters/prod/nodepool.yaml
@@ -5,3 +5,3 @@ spec:
-  version: "1.31.4-gke.100"
+  version: "1.32.1-gke.200"
`
	ghServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/example-org/example-infra/pulls/42" && r.Header.Get(acceptHeader) == acceptGitHubDiff:
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(sampleDiff))
		case r.URL.Path == "/repos/example-org/example-infra/pulls/42":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"number":        testPRNumber,
				"title":         "chore(gke): upgrade prod nodepool to 1.32.1",
				"state":         "open",
				"html_url":      testPRURL,
				"body":          "Automated GKE upgrade PR opened by kube-agents.",
				"additions":     1,
				"deletions":     1,
				"changed_files": 1,
				"user":          map[string]any{"login": "kube-agents-bot"},
				"head":          map[string]any{"ref": "upgrade-1.32"},
				"base":          map[string]any{"ref": "main"},
			})
		case r.URL.Path == "/repos/example-org/example-infra/pulls/42/files":
			_ = json.NewEncoder(w).Encode([]map[string]any{
				{
					"filename":  "clusters/prod/nodepool.yaml",
					"additions": 1,
					"deletions": 1,
					"status":    "modified",
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(ghServer.Close)

	tmpDir := t.TempDir()
	store, err := NewConfigStore(filepath.Join(tmpDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store, APIBaseURL: ghServer.URL})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	outDir := filepath.Join(tmpDir, "pr-out")
	args, _ := json.Marshal(map[string]any{
		"pr":         testPRURL,
		"output_dir": outDir,
	})
	res := mcpSrv.CallTool(context.Background(), toolInspectPR, args)
	if res.IsError {
		t.Fatalf("inspect_pr failed: %s", res.Content[0].Text)
	}
	text := res.Content[0].Text
	if !strings.Contains(text, "file://") || !strings.Contains(text, "clusters/prod/nodepool.yaml") {
		t.Errorf("inspect_pr output missing file:// links or filename:\n%s", text)
	}
	if _, err := os.Stat(filepath.Join(outDir, "pr-42.diff")); err != nil {
		t.Errorf("expected pr-42.diff on disk: %v", err)
	}
	if _, err := os.Stat(filepath.Join(outDir, "files", "clusters__prod__nodepool.yaml.patch")); err != nil {
		t.Errorf("expected per-file patch on disk: %v", err)
	}
}

func seedBusTopicsAndCard(t *testing.T, nc *nats.Conn) {
	t.Helper()
	cardEnv := lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "card-1",
		TS:         time.Now().UTC(),
		Kind:       lib.KindAgentCard,
		From:       lib.Party{Session: testPlatformAddressee},
		Payload:    mustJSON(map[string]any{"name": testPlatformAddressee, "version": "0.4.0", "description": "GKE Fleet Platform Agent"}),
	}
	publishTaskEnv(t, nc, "a2a.agents.platform.card", cardEnv)

	topicArt, err := lib.NewTopicArtifact("upgrade-readiness", "Cluster is ready for 1.32", map[string]any{"ready": true, "targetVersion": "1.32.1"})
	if err != nil {
		t.Fatal(err)
	}
	topicEnv := lib.Envelope{
		Protocol:      lib.Protocol,
		EnvelopeID:    "topic-1",
		TS:            time.Now().UTC(),
		CorrelationID: "corr-1",
		Kind:          lib.KindTopicUpdate,
		From:          lib.Party{Session: testPlatformAddressee},
		Payload:       mustJSON(topicArt),
	}
	publishTaskEnv(t, nc, "a2a.topics.state.agent.platform.upgrade-readiness", topicEnv)
	_ = nc.Flush()
}

func startSimulatedGatewayAndWorkers(t *testing.T, nc *nats.Conn) {
	t.Helper()
	sub, err := nc.Subscribe("chat.console.*.in", func(msg *nats.Msg) {
		token := strings.TrimPrefix(msg.Subject, subjectConsoleInPrefix)
		token = strings.TrimSuffix(token, subjectConsoleInSuffix)
		var frame ConsoleInFrame
		if json.Unmarshal(msg.Data, &frame) != nil {
			return
		}
		if frame.Text == cmdSessionOn {
			out, _ := json.Marshal(ConsoleOutFrame{
				MessageID: "out-1",
				Text:      "session started — `" + testSessionAddressee + "`",
			})
			_ = nc.Publish(subjectConsoleOutPrefix+token+subjectConsoleOutSuffix, out)
			return
		}

		conv := consoleConvPrefix + token
		authRaw := mustJSON(map[string]any{
			"audience": map[string]any{"conversation": conv},
		})
		now := time.Now().UTC()

		// 1. Parent session worker task.
		inSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testSessionAddressee, testParentTaskID)
		outSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testSessionAddressee, testParentTaskID)

		publishTaskEnv(t, nc, inSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-1",
			TS:         now,
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  authRaw,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-p-1", Parts: []lib.Part{{Kind: "text", Text: frame.Text}}}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-2",
			TS:         now.Add(10 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testParentTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateWorking}}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-3",
			TS:         now.Add(20 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testParentTaskID,
				ContextID: testContextID,
				Append:    true,
				Artifact:  lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Analyzing cluster upgrade readiness and delegating to platform agent."}}},
			}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-4",
			TS:         now.Add(30 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testParentTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"run_kubectl","input":"get nodes","call_id":"c1","status":"ok","duration_ms":85}`)}}},
			}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-5",
			TS:         now.Add(40 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testParentTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"addressee":"platform","text":"verify upgrade readiness and open PR"}`)}}},
			}),
		})
		publishTaskEnv(t, nc, outSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-p-6",
			TS:         now.Add(50 * time.Millisecond),
			TaskID:     testParentTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testParentTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		})

		// 2. Delegated child task on platform.
		childAuth := mustJSON(map[string]any{
			"audience":      map[string]any{"conversation": conv},
			"delegatedFrom": map[string]any{"taskId": testParentTaskID, "contextId": testContextID, "agent": testSessionAddressee},
		})
		cInSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testPlatformAddressee, testChildTaskID)
		cOutSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testPlatformAddressee, testChildTaskID)
		publishTaskEnv(t, nc, cInSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-1",
			TS:         now.Add(60 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  childAuth,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-c-1", Parts: []lib.Part{{Kind: "text", Text: "verify upgrade readiness and open PR"}}}),
		})
		publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-2",
			TS:         now.Add(70 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testPlatformAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testChildTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactNotice, Parts: []lib.Part{{Kind: "text", Text: "Opened PR " + testPRURL}}},
			}),
		})
		publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-3",
			TS:         now.Add(80 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testPlatformAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testChildTaskID,
				ContextID: testContextID,
				Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "PR #42 opened for 1.32 upgrade."}}},
			}),
		})
		publishTaskEnv(t, nc, cOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-c-4",
			TS:         now.Add(90 * time.Millisecond),
			TaskID:     testChildTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testPlatformAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testChildTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		})

		// 3. Session wake task synthesizing the child's result.
		wInSubj := fmt.Sprintf("a2a.tasks.%s.%s.in", testSessionAddressee, testWakeTaskID)
		wOutSubj := fmt.Sprintf("a2a.tasks.%s.%s.out", testSessionAddressee, testWakeTaskID)
		publishTaskEnv(t, nc, wInSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-w-1",
			TS:         now.Add(100 * time.Millisecond),
			TaskID:     testWakeTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  authRaw,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-w-1", Parts: []lib.Part{{Kind: "text", Text: "[delegated task " + testChildTaskID + " completed]\n\nPR #42 opened."}}}),
		})
		chatPayload := mustJSON(chatmsg.Message{
			Schema:   chatmsg.Schema,
			Headline: "Upgrade Readiness Confirmed & PR Opened",
			Tone:     chatmsg.ToneSuccess,
			Note:     "GKE 1.32.1",
			Detail:   "All pre-flight checks passed and PR #42 has been created.",
			Rows: []chatmsg.Row{
				{Text: "Control Plane 1.32.1-gke.200", Severity: severityGood, Detail: "Compatible"},
			},
			Plan: &chatmsg.Plan{
				Title: "Upgrade Rollout",
				Items: []chatmsg.PlanItem{
					{ID: "step-1", Title: "Verify deprecations", Status: chatmsg.StatusComplete},
					{ID: "step-2", Title: "Open GitOps PR #42", Status: chatmsg.StatusComplete},
				},
			},
			Fold: &chatmsg.Fold{
				Title: "Node Pool Breakdown",
				Table: &chatmsg.Table{
					Columns: []string{"Pool", "Nodes", "Status"},
					Rows:    [][]string{{"default-pool", "3", "Ready"}},
				},
			},
			Choices: []chatmsg.Choice{
				{Text: "Merge PR #42", Reply: "merge PR #42"},
			},
			Links: []chatmsg.Link{
				{Text: "PR #42", URL: testPRURL},
			},
		})
		publishTaskEnv(t, nc, wOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-w-2",
			TS:         now.Add(110 * time.Millisecond),
			TaskID:     testWakeTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    testWakeTaskID,
				ContextID: testContextID,
				Artifact: lib.Artifact{
					Name: lib.ArtifactResult,
					Parts: []lib.Part{
						{Kind: "text", Text: "Upgrade Readiness Confirmed & PR Opened"},
						{Kind: "data", Data: chatPayload},
					},
				},
			}),
		})
		publishTaskEnv(t, nc, wOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "env-w-3",
			TS:         now.Add(120 * time.Millisecond),
			TaskID:     testWakeTaskID,
			ContextID:  testContextID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: testSessionAddressee},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: testWakeTaskID, ContextID: testContextID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		})
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

func TestWireActivityObjectAndConsoleBoolEdit(t *testing.T) {
	item, ok := parseActivityItem(`{"tool":"mcp__gke__list_clusters","input":{"parent":"\u003cstring, 35 chars\u003e"},"callId":"call-99","status":"completed","durationMs":2337}`)
	if !ok {
		t.Fatal("expected parseActivityItem to succeed on JSON object input")
	}
	if item.Tool != "mcp__gke__list_clusters" || item.CallID != "call-99" || item.DurationMs != 2337 {
		t.Fatalf("unexpected parsed ActivityItem: %+v", item)
	}
	if !strings.Contains(item.Input, "<string, 35 chars>") {
		t.Fatalf("expected unescaped angle brackets in Input, got %q", item.Input)
	}

	s := &BusSession{}
	s.ingestConsoleOut("chat.console.tok1.out", []byte(`{"messageId":"m-1","text":"⏳ submitted…"}`))
	s.ingestConsoleOut("chat.console.tok1.out", []byte(`{"messageId":"m-1","text":"🔧 running kubectl get nodes…","edit":true}`))
	notices := s.consoleNoticesSince("tok1", 0)
	if len(notices) != 1 || notices[0].Text != "🔧 running kubectl get nodes…" {
		t.Fatalf("expected in-place edited console notice, got %+v", notices)
	}
}

func TestProgressNotificationsAndAutoMetaBinding(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
	ctx := context.Background()
	startSimulatedGatewayAndWorkers(t, nc)

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	connectArgs, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv.ClientURL(),
	})
	_ = mcpSrv.CallTool(ctx, toolConnectDeployment, connectArgs)

	// 1. Default mode (debug=false): suppresses notifications/progress so Jetski's tool card stays collapsed by default,
	// and wraps output in _custom_tool_result with collapsed <details><summary>Thought...</summary></details>.
	callLineDefault := `{"jsonrpc":"2.0","id":77,"method":"tools/call","params":{"name":"send_message","arguments":{"message":"Check upgrade readiness","mode":"session","wait_timeout_seconds":10},"_meta":{"progressToken":"mcp-progress-77","antigravity.google/conversation_id":"conv-auto-bound-77"}}}` + "\n"
	var stdioOutDefault bytes.Buffer
	if err := mcpSrv.ServeStdio(ctx, strings.NewReader(callLineDefault), &stdioOutDefault); err != nil {
		t.Fatalf("ServeStdio default: %v", err)
	}
	outputDefault := stdioOutDefault.String()
	if strings.Contains(outputDefault, `"method":"notifications/progress"`) {
		t.Fatalf("expected NO notifications/progress frames when debug=false (keeps tool card collapsed), got:\n%s", outputDefault)
	}
	if !strings.Contains(outputDefault, `_custom_tool_result`) {
		t.Fatalf("expected _custom_tool_result wrapper in stdio output, got:\n%s", outputDefault)
	}
	if gotConv := store.ResolveJetskiConversationID(); gotConv != "conv-auto-bound-77" {
		t.Fatalf("ResolveJetskiConversationID = %q, want conv-auto-bound-77", gotConv)
	}

	// 2. Debug mode (debug=true): emits live MCP notifications/progress frames with Thinking and Tool Activity.
	srv2, nc2, _ := startEmbeddedJetStream(t)
	startSimulatedGatewayAndWorkers(t, nc2)
	store2, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config-2.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv2, err := NewServer(ServerOptions{Store: store2})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv2.Close)
	connectArgs2, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv2.ClientURL(),
	})
	_ = mcpSrv2.CallTool(ctx, toolConnectDeployment, connectArgs2)

	callLineDebug := `{"jsonrpc":"2.0","id":78,"method":"tools/call","params":{"name":"send_message","arguments":{"message":"Check upgrade readiness","mode":"session","wait_timeout_seconds":10,"debug":true},"_meta":{"progressToken":"mcp-progress-78"}}}` + "\n"
	var stdioOutDebug bytes.Buffer
	if err := mcpSrv2.ServeStdio(ctx, strings.NewReader(callLineDebug), &stdioOutDebug); err != nil {
		t.Fatalf("ServeStdio debug: %v", err)
	}
	outputDebug := stdioOutDebug.String()
	if !strings.Contains(outputDebug, `"method":"notifications/progress"`) || !strings.Contains(outputDebug, `"progressToken":"mcp-progress-78"`) {
		t.Fatalf("expected MCP notifications/progress frames in stdio output when debug=true, got:\n%s", outputDebug)
	}
	if !strings.Contains(outputDebug, "Task t-parent-101") || !strings.Contains(outputDebug, "Tool Activity Stream") {
		t.Fatalf("expected live task progress and Tool Activity Stream when debug=true, got:\n%s", outputDebug)
	}
}

func TestIncrementalStepStreamingAndCustomEnvelope(t *testing.T) {
	srv, nc, _ := startEmbeddedJetStream(t)
	ctx := context.Background()

	const (
		multiStepTaskID = "task-multistep-501"
		multiStepCtxID  = "ctx-multistep-501"
	)

	// Simulate a multi-step platform-agent task where:
	// - Step 1 & 2 arrive within 200ms (so send_message returns after ~1.3s with STREAMING_STEP_CONTINUE).
	// - Step 3 and final completion arrive ~2.0s after task start (captured by the follow-up task_control get call).
	sub, err := nc.Subscribe("chat.console.*.in", func(msg *nats.Msg) {
		var in ConsoleInFrame
		_ = json.Unmarshal(msg.Data, &in)
		token := strings.TrimPrefix(msg.Subject, subjectConsoleInPrefix)
		token = strings.TrimSuffix(token, subjectConsoleInSuffix)
		convName := consoleConvPrefix + token
		authRaw := mustJSON(map[string]any{
			"audience": map[string]any{"conversation": convName},
		})
		now := time.Now().UTC()

		pReqSubj := fmt.Sprintf("a2a.tasks.platform.%s.in", multiStepTaskID)
		pOutSubj := fmt.Sprintf("a2a.tasks.platform.%s.out", multiStepTaskID)

		publishTaskEnv(t, nc, pReqSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-req-1",
			TS:         now,
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindMessage,
			From:       lib.Party{Session: "gateway"},
			Authority:  authRaw,
			Payload:    mustJSON(lib.Message{Role: "user", MessageID: "m-ms-1", Parts: []lib.Part{{Kind: "text", Text: in.Text}}}),
		})
		publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-out-1",
			TS:         now.Add(20 * time.Millisecond),
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindStatusUpdate,
			From:       lib.Party{Session: "platform-agent"},
			Payload:    mustJSON(lib.StatusUpdate{TaskID: multiStepTaskID, ContextID: multiStepCtxID, Status: lib.TaskStatus{State: lib.StateWorking}}),
		})
		publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-out-2",
			TS:         now.Add(50 * time.Millisecond),
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: "platform-agent"},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    multiStepTaskID,
				ContextID: multiStepCtxID,
				Append:    true,
				Artifact:  lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Searching available tools.\nListing GKE clusters in the fleet."}}},
			}),
		})

		go func() {
			time.Sleep(1800 * time.Millisecond)
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-3",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    multiStepTaskID,
					ContextID: multiStepCtxID,
					Append:    true,
					Artifact:  lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Running cluster and workspace diagnostics."}}},
				}),
			})
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-4",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    multiStepTaskID,
					ContextID: multiStepCtxID,
					Artifact:  lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "All 9 pods in kubeagents-system are Running and Ready."}}},
				}),
			})
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-5",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindStatusUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload:    mustJSON(lib.StatusUpdate{TaskID: multiStepTaskID, ContextID: multiStepCtxID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
			})
		}()
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	connectArgs, _ := json.Marshal(map[string]any{
		"deployment":      testClusterName,
		"namespace":       testNamespace,
		"direct_nats_url": srv.ClientURL(),
	})
	_ = mcpSrv.CallTool(ctx, toolConnectDeployment, connectArgs)

	// 1. Call send_message over JSON-RPC with Jetski _meta: should return Step 1 ONLY (not a cumulative list!)
	req1 := `{"jsonrpc":"2.0","id":201,"method":"tools/call","params":{"name":"send_message","arguments":{"message":"Check all pods in kubeagents-system"},"_meta":{"antigravity.google/conversation_id":"conv-stream-test"}}}`
	resp1Raw := mcpSrv.HandleJSONRPC(ctx, []byte(req1))
	var resp1 struct {
		Result mcpCallToolResult `json:"result"`
	}
	if err := json.Unmarshal(resp1Raw, &resp1); err != nil {
		t.Fatalf("unmarshal resp1: %v", err)
	}
	var env1 struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(resp1.Result.Content[0].Text), &env1); err != nil {
		t.Fatalf("unmarshal env1: %v\nraw: %s", err, resp1.Result.Content[0].Text)
	}
	if !env1.Custom {
		t.Fatalf("expected _custom_tool_result=true in env1")
	}
	if !strings.Contains(env1.Title, "Searching available tools") {
		t.Errorf("unexpected step 1 title: %q", env1.Title)
	}
	if !strings.Contains(env1.MD, "✅ **Searching available tools.**") || strings.Contains(env1.MD, "Listing GKE clusters") {
		t.Errorf("expected step 1 markdown to contain ONLY step 1:\n%s", env1.MD)
	}
	if !strings.Contains(env1.Result, "STREAMING_STEP_CONTINUE") || !strings.Contains(env1.Result, multiStepTaskID) {
		t.Errorf("expected STREAMING_STEP_CONTINUE in LLM result:\n%s", env1.Result)
	}

	// 2. Call task_control(action: "get") #1: advances by 1 step to Step 2 ("Listing GKE clusters in the fleet")
	// and does NOT repeat Step 1 in its markdown!
	req2 := `{"jsonrpc":"2.0","id":202,"method":"tools/call","params":{"name":"task_control","arguments":{"action":"get","task_id":"` + multiStepTaskID + `"},"_meta":{"antigravity.google/conversation_id":"conv-stream-test"}}}`
	resp2Raw := mcpSrv.HandleJSONRPC(ctx, []byte(req2))
	var resp2 struct {
		Result mcpCallToolResult `json:"result"`
	}
	if err := json.Unmarshal(resp2Raw, &resp2); err != nil {
		t.Fatalf("unmarshal resp2: %v", err)
	}
	var env2 struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(resp2.Result.Content[0].Text), &env2); err != nil {
		t.Fatalf("unmarshal env2: %v", err)
	}
	if !strings.Contains(env2.Title, "Listing GKE clusters in the fleet") {
		t.Errorf("unexpected step 2 title: %q", env2.Title)
	}
	if !strings.Contains(env2.MD, "✅ **Listing GKE clusters in the fleet.**") || strings.Contains(env2.MD, "Searching available tools") {
		t.Errorf("expected step 2 markdown to contain ONLY step 2 (not repeating step 1):\n%s", env2.MD)
	}

	// 3. Call task_control(action: "get") #2: waits for step 3 & completion and returns ONLY step 3 in the step row
	// (no duplicate cumulative list!), while the LLM result has the full <details><summary>Thought...(3 steps)</summary> + answer.
	req3 := `{"jsonrpc":"2.0","id":203,"method":"tools/call","params":{"name":"task_control","arguments":{"action":"get","task_id":"` + multiStepTaskID + `"},"_meta":{"antigravity.google/conversation_id":"conv-stream-test"}}}`
	resp3Raw := mcpSrv.HandleJSONRPC(ctx, []byte(req3))
	var resp3 struct {
		Result mcpCallToolResult `json:"result"`
	}
	if err := json.Unmarshal(resp3Raw, &resp3); err != nil {
		t.Fatalf("unmarshal resp3: %v", err)
	}
	var env3 struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		MD     string `json:"markdown"`
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(resp3.Result.Content[0].Text), &env3); err != nil {
		t.Fatalf("unmarshal env3: %v", err)
	}
	if !strings.Contains(env3.Title, "Running cluster and workspace diagnostics") {
		t.Errorf("unexpected completion step title: %q", env3.Title)
	}
	if !strings.Contains(env3.MD, "✅ **Running cluster and workspace diagnostics.**") || strings.Contains(env3.MD, "Searching available tools") {
		t.Errorf("expected completion step markdown to contain ONLY step 3 (not repeating steps 1-2):\n%s", env3.MD)
	}
	for _, want := range []string{
		"<details>\n<summary>Thought",
		"(3 steps)</summary>",
		"- ✅ Searching available tools.",
		"- ✅ Listing GKE clusters in the fleet.",
		"- ✅ Running cluster and workspace diagnostics.",
		"All 9 pods in kubeagents-system are Running and Ready.",
	} {
		if !strings.Contains(env3.Result, want) {
			t.Errorf("final LLM result missing %q:\n%s", want, env3.Result)
		}
	}
}

func publishTaskEnv(t *testing.T, nc *nats.Conn, subject string, env lib.Envelope) {
	t.Helper()
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish(subject, raw); err != nil {
		t.Fatal(err)
	}
}

func mustJSON(v any) json.RawMessage {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		panic(err)
	}
	return json.RawMessage(bytes.TrimSpace(buf.Bytes()))
}
