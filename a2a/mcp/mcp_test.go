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
	// Debug mode is scoped to the conversation (soak round 6: a global flag flipped every
	// concurrent conversation into debug output).
	if msDebugOn.IsError || !store.DebugModeFor(store.ActiveConversation().Alias) || store.DebugMode() {
		t.Fatalf("manage_session debug_on failed: %+v", msDebugOn)
	}
	if other, _ := store.EnsureConversation("unrelated-conv", true, ""); store.DebugModeFor(other.Alias) {
		t.Fatalf("debug_on must not leak to other conversations")
	}
	_, _ = store.EnsureConversation("default", false, "")
	msDebugOff := mcpSrv.CallTool(ctx, toolManageSession, json.RawMessage(`{"action":"debug_off"}`))
	if msDebugOff.IsError || store.DebugModeFor("default") {
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

	// Part C: Test HandleJetskiHook ignores unrelated conversations and injects pending events only for known kube-agents conversations.
	_ = store.AppendPendingEvents(PushEvent{
		ID:        "hook-ev-1",
		Kind:      eventKindNoticePR,
		Title:     "PR #77 Opened",
		Summary:   "Automated fix PR",
		PRLinks:   []string{"https://github.com/example-org/example-infra/pull/77"},
		Timestamp: time.Now().UTC(),
	})
	var unrelatedOut bytes.Buffer
	if err := HandleJetskiHook(store, strings.NewReader(`{"conversationId":"unrelated-coding-conv-99"}`), &unrelatedOut); err != nil {
		t.Fatalf("HandleJetskiHook (unrelated): %v", err)
	}
	if store.ResolveJetskiConversationID() != testJetskiConvID {
		t.Errorf("ResolveJetskiConversationID after unrelated hook = %q, want %q", store.ResolveJetskiConversationID(), testJetskiConvID)
	}
	if strings.TrimSpace(unrelatedOut.String()) != "{}" {
		t.Errorf("expected unrelated hook to not drain events, got: %s", unrelatedOut.String())
	}

	var hookOut bytes.Buffer
	if err := HandleJetskiHook(store, strings.NewReader(fmt.Sprintf(`{"conversationId":%q}`, testJetskiConvID)), &hookOut); err != nil {
		t.Fatalf("HandleJetskiHook: %v", err)
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
				Artifact:  lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Searching available tools.\tLooking up GKE and kubectl tools first.\nListing GKE clusters in the fleet.\tQuerying fleet cluster inventory."}}},
			}),
		})
		publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
			Protocol:   lib.Protocol,
			EnvelopeID: "ms-out-2b",
			TS:         now.Add(60 * time.Millisecond),
			TaskID:     multiStepTaskID,
			ContextID:  multiStepCtxID,
			Kind:       lib.KindArtifactUpdate,
			From:       lib.Party{Session: "platform-agent"},
			Payload: mustJSON(lib.ArtifactUpdate{
				TaskID:    multiStepTaskID,
				ContextID: multiStepCtxID,
				Artifact:  lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"tool_search","input":{"query":"\u003cstring, 11 chars\u003e"},"callId":"c-ts-1","status":"completed","durationMs":45}`)}, {Kind: "data", Data: json.RawMessage(`{"tool":"mcp__gke__list_clusters","input":{"project":"\u003cstring, 14 chars\u003e"},"callId":"c-gke-1","status":"completed","durationMs":210}`)}}},
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
					Artifact:  lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Checking Kubernetes node status and readiness.\tAll 5 node pools RUNNING, now checking pods and nodes."}}},
				}),
			})
			publishTaskEnv(t, nc, pOutSubj, lib.Envelope{
				Protocol:   lib.Protocol,
				EnvelopeID: "ms-out-3b",
				TS:         time.Now().UTC(),
				TaskID:     multiStepTaskID,
				ContextID:  multiStepCtxID,
				Kind:       lib.KindArtifactUpdate,
				From:       lib.Party{Session: "platform-agent"},
				Payload: mustJSON(lib.ArtifactUpdate{
					TaskID:    multiStepTaskID,
					ContextID: multiStepCtxID,
					Append:    true,
					Artifact:  lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"terminal","input":{"command":"\u003cstring, 48 chars\u003e"},"callId":"c-term-1","status":"completed","durationMs":620}`)}}},
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

	// 1. Call send_message over JSON-RPC with Jetski _meta: should return Step 1 ONLY with rich Agent + Thought + Tool Activity!
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
	for _, wantInMD1 := range []string{
		"**Agent**: `platform`",
		"**Thought**: *Looking up GKE and kubectl tools first.*",
		"**`tool_search`**",
		"45ms",
	} {
		if !strings.Contains(env1.MD, wantInMD1) {
			t.Errorf("step 1 expanded markdown missing %q:\n%s", wantInMD1, env1.MD)
		}
	}
	if strings.Contains(env1.MD, "Listing GKE clusters") {
		t.Errorf("expected step 1 markdown to contain ONLY step 1:\n%s", env1.MD)
	}
	if !strings.Contains(env1.Result, "STREAMING_STEP_CONTINUE") || !strings.Contains(env1.Result, multiStepTaskID) {
		t.Errorf("expected STREAMING_STEP_CONTINUE in LLM result:\n%s", env1.Result)
	}

	// 2. Call task_control(action: "get") #1: advances by 1 step to Step 2 ("Listing GKE clusters in the fleet")
	// and shows Step 2's Thought + Tool Activity!
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
	for _, wantInMD2 := range []string{
		"**Thought**: *Querying fleet cluster inventory.*",
		"**`mcp__gke__list_clusters`**",
		"210ms",
	} {
		if !strings.Contains(env2.MD, wantInMD2) {
			t.Errorf("step 2 expanded markdown missing %q:\n%s", wantInMD2, env2.MD)
		}
	}
	if strings.Contains(env2.MD, "Searching available tools") {
		t.Errorf("expected step 2 markdown to contain ONLY step 2 (not repeating step 1):\n%s", env2.MD)
	}

	// 3. Call task_control(action: "get") #2: waits for step 3 & completion and returns ONLY step 3 in the step row,
	// while the LLM result has ONLY the final answer (NO duplicate <details><summary>Thought...</summary> block!).
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
	if !strings.Contains(env3.Title, "Checking Kubernetes node status and readiness") {
		t.Errorf("unexpected completion step title: %q", env3.Title)
	}
	for _, wantInMD3 := range []string{
		"**Thought**: *All 5 node pools RUNNING, now checking pods and nodes.*",
		"**`terminal`**",
		"`command` (48 chars)",
		"620ms",
	} {
		if !strings.Contains(env3.MD, wantInMD3) {
			t.Errorf("step 3 expanded markdown missing %q:\n%s", wantInMD3, env3.MD)
		}
	}
	if !strings.Contains(env3.Result, "All 9 pods in kubeagents-system are Running and Ready.") {
		t.Errorf("final LLM result missing answer:\n%s", env3.Result)
	}
	if strings.Contains(env3.Result, "<details>\n<summary>Thought") {
		t.Errorf("final LLM result in Jetski custom envelope should NOT contain duplicate <details><summary>Thought block:\n%s", env3.Result)
	}
}

func TestViaAuthorityDelegationAndConversationalStepNormalization(t *testing.T) {
	s := &BusSession{
		tasks:         make(map[string]*TaskRecord),
		seenEnvelopes: make(map[string]bool),
	}
	now := time.Now().UTC()

	// 1. Ingest parent /session task that emits ArtifactDelegate to platform.
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-parent-via.in", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-1",
		TS:         now,
		TaskID:     "task-parent-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindMessage,
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:tok"}}),
		Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "check all clusters"}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-parent-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-2",
		TS:         now.Add(1 * time.Second),
		TaskID:     "task-parent-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:   "task-parent-via",
			Artifact: lib.Artifact{Name: lib.ArtifactDelegate, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"addressee":"platform","text":"Run fleet health check across 16 clusters"}`)}}},
		}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-parent-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-3",
		TS:         now.Add(2 * time.Second),
		TaskID:     "task-parent-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-parent-via", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	// 2. Ingest delegated child task on platform using gateway's real `authority.via.taskId` wire format,
	// with conversational thinking lines and an intermediate "Synthesizing findings into final response." before Kanban steps.
	s.ingestTaskEnvelope("a2a.tasks.platform.task-child-via.in", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-4",
		TS:         now.Add(3 * time.Second),
		TaskID:     "task-child-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindMessage,
		Authority: mustJSON(map[string]any{
			"audience": map[string]any{"conversation": "console:tok"},
			"via":      map[string]any{"taskId": "task-parent-via", "session": "chat-puffin-1"},
		}),
		Payload: mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "Run fleet health check across 16 clusters"}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.platform.task-child-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-5",
		TS:         now.Add(10 * time.Second),
		TaskID:     "task-child-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: "task-child-via",
			Append: true,
			Artifact: lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: strings.Join([]string{
				"Listing GKE cluster profiles in the fleet.",
				"Synthesizing findings into final response.",
				"Preflight ok. Now let's run diagnostics",
				"12/16 done, 4 still running. Wait 25s and re-check.",
				"Synthesizing findings into final response.",
			}, "\n")}}},
		}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.platform.task-child-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-6",
		TS:         now.Add(20 * time.Second),
		TaskID:     "task-child-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-child-via", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	// 3. Ingest gateway's real wake synthesis prompt ("The task you delegated to platform (task task-child-via) returned:...").
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-wake-via.in", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-7",
		TS:         now.Add(21 * time.Second),
		TaskID:     "task-wake-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindMessage,
		Authority:  mustJSON(map[string]any{"audience": map[string]any{"conversation": "console:tok"}}),
		Payload:    mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "You were asked:\n```\ncheck all clusters\n```\n\nYou delegated to platform (task task-child-via), which returned:\n\n16/16 clusters healthy."}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-wake-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-8",
		TS:         now.Add(25 * time.Second),
		TaskID:     "task-wake-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID:   "task-wake-via",
			Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "All 16 clusters are healthy."}}},
		}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.chat-puffin-1.task-wake-via.out", mustJSON(lib.Envelope{
		Protocol:   lib.Protocol,
		EnvelopeID: "via-9",
		TS:         now.Add(26 * time.Second),
		TaskID:     "task-wake-via",
		ContextID:  "ctx-via",
		Kind:       lib.KindStatusUpdate,
		Payload:    mustJSON(lib.StatusUpdate{TaskID: "task-wake-via", Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	if !s.IsTaskTreeSettled("task-parent-via") {
		t.Fatal("expected task tree to be settled once parent, via-child, and wake tasks complete")
	}
	outcome := s.BuildTaskOutcome("task-parent-via", "fleet-health", "tok", "check all clusters", false)
	if len(outcome.ChildTasks) != 1 || outcome.ChildTasks[0].TaskID != "task-child-via" {
		t.Fatalf("expected child task linked via authority.via.taskId, got %+v", outcome.ChildTasks)
	}
	if outcome.WakeTask == nil || outcome.WakeTask.TaskID != "task-wake-via" {
		t.Fatalf("expected wake task linked via gateway wake prompt, got %+v", outcome.WakeTask)
	}

	details := CollectOutcomeStepDetails(outcome)
	wantTitles := []string{
		"Delegating task to platform.",
		"Listing GKE cluster profiles in the fleet.",
		"Completing preflight checks and starting cluster diagnostics.",
		"Waiting for specialist agent tasks (12/16 completed).",
		"Synthesizing findings into final response.",
	}
	if len(details) != len(wantTitles) {
		t.Fatalf("got %d step details (%+v), want %d (%v)", len(details), details, len(wantTitles), wantTitles)
	}
	for i, want := range wantTitles {
		if details[i].Title != want {
			t.Errorf("step[%d].Title = %q, want %q", i, details[i].Title, want)
		}
	}
	if details[2].Thought != "Preflight ok. Now let's run diagnostics" {
		t.Errorf("step[2].Thought = %q, want conversational line preserved in Thought", details[2].Thought)
	}
	if details[3].Thought != "12/16 done, 4 still running. Wait 25s and re-check." {
		t.Errorf("step[3].Thought = %q, want progress detail preserved in Thought", details[3].Thought)
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

// TestKanbanCardTasksLinkToForegroundTaskAndStreamAsSteps reproduces the 2026-10-16 "check the fresh
// health of all my clusters" session: the platform agent fanned out 15 task-card-* specialist tasks
// in the same conversation. Before the fix they (a) fired 15 task_completed push events that were
// piggybacked onto a STREAMING_STEP_CONTINUE frame (12KB), (b) were invisible as progress, and (c)
// a steer reset the step cursor so all 18 steps re-streamed as duplicates.
func TestKanbanCardTasksLinkToForegroundTaskAndStreamAsSteps(t *testing.T) {
	var pushed []PushEvent
	s := &BusSession{
		tasks:             make(map[string]*TaskRecord),
		seenEnvelopes:     make(map[string]bool),
		foregroundTasks:   make(map[string]time.Time),
		watchedTasks:      make(map[string]int),
		watchedConvs:      make(map[string]int),
		lastReportedSteps: make(map[string]int),
		bootstrapped:      true,
		onPushEvent:       func(ev PushEvent) { pushed = append(pushed, ev) },
	}
	now := time.Now().UTC()
	conv := "console:jetski-tok"

	// Foreground platform task, being polled by task_control(get) (foreground lease, no conv watch).
	s.ingestTaskEnvelope("a2a.tasks.platform.task-fg.in", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "fg-1", TS: now, TaskID: "task-fg", ContextID: "ctx-fg",
		Kind:      lib.KindMessage,
		Authority: mustJSON(map[string]any{"audience": map[string]any{"conversation": conv}}),
		Payload:   mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "check the fresh health of all my clusters"}}}),
	}), true)
	s.ingestTaskEnvelope("a2a.tasks.platform.task-fg.out", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "fg-2", TS: now.Add(time.Second), TaskID: "task-fg", ContextID: "ctx-fg",
		Kind:    lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{TaskID: "task-fg", Artifact: lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Running kanban_create.\nWaiting for specialist agent tasks to complete."}}}}),
	}), true)
	s.markForegroundStepTask("task-fg", false)

	// Two Kanban card tasks in the same conversation, no authority.via / delegatedFrom link.
	for i, name := range []string{"drift-peer-std-9", "ap-ap-deploy-test"} {
		id := fmt.Sprintf("task-card-t-%d", i)
		base := now.Add(time.Duration(10+i) * time.Second)
		s.ingestTaskEnvelope("a2a.tasks.platform."+id+".in", mustJSON(lib.Envelope{
			Protocol: lib.Protocol, EnvelopeID: id + "-1", TS: base, TaskID: id, ContextID: "ctx-fg",
			Kind:      lib.KindMessage,
			Authority: mustJSON(map[string]any{"audience": map[string]any{"conversation": conv}}),
			Payload:   mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "Live health check for cluster " + name}}}),
		}), true)
		s.ingestTaskEnvelope("a2a.tasks.platform."+id+".out", mustJSON(lib.Envelope{
			Protocol: lib.Protocol, EnvelopeID: id + "-2", TS: base.Add(20 * time.Second), TaskID: id, ContextID: "ctx-fg",
			Kind:    lib.KindArtifactUpdate,
			Payload: mustJSON(lib.ArtifactUpdate{TaskID: id, Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "**Cluster `" + name + "` is healthy.**\n\nLive checks: 2/2 nodes Ready, 0 pods pending."}}}}),
		}), true)
		s.ingestTaskEnvelope("a2a.tasks.platform."+id+".out", mustJSON(lib.Envelope{
			Protocol: lib.Protocol, EnvelopeID: id + "-3", TS: base.Add(21 * time.Second), TaskID: id, ContextID: "ctx-fg",
			Kind:    lib.KindStatusUpdate,
			Payload: mustJSON(lib.StatusUpdate{TaskID: id, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
		}), true)
	}

	if got := s.GetTask("task-card-t-0").CardParentTaskID; got != "task-fg" {
		t.Fatalf("card task not linked to foreground task: CardParentTaskID=%q", got)
	}
	if len(pushed) != 0 {
		t.Errorf("card completions must not fire push events while the parent is in the foreground; got %d: %+v", len(pushed), pushed)
	}

	outcome := s.BuildTaskOutcome("task-fg", "default", "jetski-tok", "check the fresh health of all my clusters", true)
	if len(outcome.CardTasks) != 2 {
		t.Fatalf("expected 2 card tasks on outcome, got %d", len(outcome.CardTasks))
	}
	steps := CollectOutcomeSteps(outcome)
	joined := strings.Join(steps, "\n")
	for _, want := range []string{
		"Specialist check completed: Cluster `drift-peer-std-9` is healthy.",
		"Specialist check completed: Cluster `ap-ap-deploy-test` is healthy.",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("card progress missing from steps; want %q in:\n%s", want, joined)
		}
	}
	details := CollectOutcomeStepDetails(outcome)
	var cardMD string
	for _, d := range details {
		if strings.HasPrefix(d.Title, "Specialist check completed: Cluster `drift-peer-std-9`") {
			cardMD = FormatStepDetailMarkdown(d, 30, true)
		}
	}
	if !strings.Contains(cardMD, "(task `task-card-t-0`)") || !strings.Contains(cardMD, "2/2 nodes Ready") {
		t.Errorf("expanded card step should carry card task id and result thought:\n%s", cardMD)
	}

	// STREAMING_STEP_CONTINUE frame with no new step must say so and report card progress.
	s.recordStepProgress("task-fg", len(steps), len(steps))
	outcome = s.BuildTaskOutcome("task-fg", "default", "jetski-tok", "", true)
	_, _, llm := FormatTurnCustomToolEnvelope(outcome, false)
	for _, want := range []string{streamingStepContinuePrefix, "No new step since the last poll", "Specialist cards: 2/2 completed", "Do NOT call `list`, `steer`"} {
		if !strings.Contains(llm, want) {
			t.Errorf("no-new-step frame missing %q:\n%s", want, llm)
		}
	}

	// Piggyback is bounded.
	var many []PushEvent
	for i := 0; i < 15; i++ {
		many = append(many, PushEvent{Kind: eventKindTaskCompleted, Title: fmt.Sprintf("card %d", i), Summary: strings.Repeat("x", 900)})
	}
	md := FormatPushEventsMarkdownBounded(many, maxPiggybackEvents, piggybackSummaryMaxChars)
	if strings.Count(md, "task_completed") != maxPiggybackEvents || !strings.Contains(md, "+ 9 earlier events omitted") || len(md) > 4000 {
		t.Errorf("bounded piggyback not bounded (len=%d, events=%d):\n%s", len(md), strings.Count(md, "task_completed"), md)
	}
}

// TestConcurrentJetskiConversationsGetIsolatedConsoleConversations reproduces soak round 1:
// Jetski conversation A ("connect", then "when was the last fleet-audit run?") and Jetski
// conversation B (`conversation: "soak-r1b"`, long fleet health check) share one MCP server
// process. B's explicit alias became the global active conversation, so A's next send_message
// (no `conversation` arg) landed on B's console token, was absorbed as a steer by B's in-flight
// task, and A streamed B's task for 12 minutes.
func TestConcurrentJetskiConversationsGetIsolatedConsoleConversations(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{store: store}
	ctxA := withCallMeta(context.Background(), rpcCallToolMeta{ConversationID: "aaaaaaaa-1111-4111-8111-111111111111"})
	ctxB := withCallMeta(context.Background(), rpcCallToolMeta{ConversationID: "bbbbbbbb-2222-4222-8222-222222222222"})
	enabled := true
	// HandleJSONRPC records the push binding for every _meta-carrying call before dispatch; replay
	// that pre-step so the test covers the real request path (it used to steal the active
	// conversation for the newest Jetski id, which is exactly how round 2 re-hijacked).
	preStep := func(ctx context.Context) {
		_, _ = store.UpdatePushConfig(callMetaFromContext(ctx).ConversationID, &enabled, "")
	}

	// A connects: binds the default conversation.
	preStep(ctxA)
	a1 := s.conversationFor(ctxA, "", "")
	if a1.Alias != defaultConvAlias {
		t.Fatalf("A should bind the default conversation, got %q", a1.Alias)
	}
	// B explicitly uses its own alias (becomes the global active conversation).
	preStep(ctxB)
	b1 := s.conversationFor(ctxB, "soak-r1b", "")
	if b1.Alias != "soak-r1b" || store.ActiveConversation().Alias != "soak-r1b" {
		t.Fatalf("B should use soak-r1b and make it active, got %q / active %q", b1.Alias, store.ActiveConversation().Alias)
	}
	// A's next message with no conversation arg must stay on A's conversation, not B's.
	preStep(ctxA)
	a2 := s.conversationFor(ctxA, "", "")
	if a2.Alias != a1.Alias || a2.Token != a1.Token {
		t.Fatalf("A hijacked onto %q (token %s); want %q (token %s)", a2.Alias, a2.Token, a1.Alias, a1.Token)
	}
	// And B keeps its own.
	preStep(ctxB)
	b2 := s.conversationFor(ctxB, "", "")
	if b2.Alias != "soak-r1b" {
		t.Fatalf("B lost its conversation: %q", b2.Alias)
	}
	// A third Jetski conversation arriving while the active conversation is bound elsewhere gets a
	// fresh auto alias rather than stealing.
	ctxC := withCallMeta(context.Background(), rpcCallToolMeta{ConversationID: "cccccccc-3333-4333-8333-333333333333"})
	preStep(ctxC)
	c1 := s.conversationFor(ctxC, "", "")
	if c1.Alias != jetskiAliasPrefix+"cccccccc" || c1.Token == a1.Token || c1.Token == b1.Token {
		t.Fatalf("C should get an isolated auto-bound conversation, got %q (token %s)", c1.Alias, c1.Token)
	}
	// Push routing for events on A's console token resolves back to Jetski conversation A.
	if got := store.ResolveJetskiConversationIDForEvent("console:" + a1.Token); got != "aaaaaaaa-1111-4111-8111-111111111111" {
		t.Fatalf("event routing for A's token resolved to %q", got)
	}
	// Non-Jetski callers (no _meta) still use the active conversation.
	plain := s.conversationFor(context.Background(), "", "")
	if plain.Alias != store.ActiveConversation().Alias {
		t.Fatalf("plain caller should get the active conversation, got %q", plain.Alias)
	}
	// Switching A explicitly moves A's binding (one console conversation per Jetski conversation).
	s.conversationFor(ctxA, "fleet-health", "")
	if got := s.conversationFor(ctxA, "", "").Alias; got != "fleet-health" {
		t.Fatalf("after explicit switch A should resolve to fleet-health, got %q", got)
	}
	if got := store.Snapshot().Conversations[defaultConvAlias].JetskiConversationID; got != "" {
		t.Fatalf("old binding on %q should have been released, still %q", a1.Alias, got)
	}
}

// TestStepCursorSurvivesSessionRestart reproduces soak round 4 (T13): Jetski relaunches the MCP
// server after a crash/upgrade and the fresh process re-streamed all 17 already-shown steps as
// duplicate rows ("1 step recorded" right after "17 steps recorded") because the cursor lived
// only in memory. It also pins CreatedAt back-dating when live delivery races the bootstrap.
func TestStepCursorSurvivesSessionRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "step-progress.json")
	s1 := &BusSession{tasks: make(map[string]*TaskRecord), seenEnvelopes: make(map[string]bool)}
	s1.EnableStepProgressPersistence(path)
	s1.recordStepProgress("task-x", 16, 17)
	s1.recordStepProgress("task-y", 0, 3)

	s2 := &BusSession{tasks: make(map[string]*TaskRecord), seenEnvelopes: make(map[string]bool)}
	s2.EnableStepProgressPersistence(path)
	if got := s2.getReportedStepCount("task-x"); got != 17 {
		t.Fatalf("cursor for task-x not restored: got %d want 17", got)
	}
	if prev, rep := s2.getStepProgress("task-y"); prev != 0 || rep != 3 {
		t.Fatalf("cursor for task-y not restored: got %d/%d", prev, rep)
	}

	// Live envelope first (TS now), then the earlier history arrives via bootstrap.
	now := time.Now().UTC()
	s2.ingestTaskEnvelope("a2a.tasks.platform.task-late.out", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "late-2", TS: now, TaskID: "task-late", ContextID: "ctx",
		Kind:    lib.KindStatusUpdate,
		Payload: mustJSON(lib.StatusUpdate{TaskID: "task-late", Status: lib.TaskStatus{State: lib.StateWorking}}),
	}), true)
	s2.ingestTaskEnvelope("a2a.tasks.platform.task-late.in", mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "late-1", TS: now.Add(-3 * time.Minute), TaskID: "task-late", ContextID: "ctx",
		Kind:    lib.KindMessage,
		Payload: mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "hi"}}}),
	}), false)
	if got := s2.GetTask("task-late").CreatedAt; !got.Equal(now.Add(-3 * time.Minute)) {
		t.Fatalf("CreatedAt should be back-dated to the submission, got %v", got)
	}
}

func TestNormalAndDebugModeStreamingAndPanels(t *testing.T) {
	now := time.Now().UTC()
	rec := &TaskRecord{
		TaskID:         "task-debug-panel-1",
		ContextID:      "ctx-debug-panel-1",
		Addressee:      "platform",
		Conversation:   "console:tok-dbg",
		State:          lib.StateWorking,
		Terminal:       false,
		CreatedAt:      now.Add(-45 * time.Second),
		UpdatedAt:      now,
		EnvelopeCount:  24,
		SteerTimestamps: []time.Time{now.Add(-15 * time.Second)},
		EnvelopeCountsByKind: map[string]int{
			"message":         1,
			"status-update":   2,
			"artifact-update": 21,
		},
		Thinking: strings.Join([]string{
			"Reading skill gke-cluster-diagnostics/SKILL.md.\tLoading diagnostic playbook.",
			"Dispatching 2 specialist tasks on the Kanban board.\tCreating specialist cards for drift-peer-ap-1 and kube-agents-host.",
			"Reading skill gke-cluster-diagnostics/SKILL.md.\tRe-checking reference section after card dispatch.",
			"Specialist check completed: drift-peer-ap-1 (healthy — 1/2 cards done).\tCluster RUNNING (v1.34.1-gke.2037001), 0 pod restarts.",
		}, "\n"),
		Activities: []ActivityItem{
			{
				Index:         1,
				Tool:          "read_file",
				Input:         `{"path":"/var/lib/hermes/skills/gke-cluster-diagnostics/SKILL.md"}`,
				CallID:        "c1",
				Status:        "ok",
				ResultPreview: "# GKE Cluster Diagnostics Skill (112 lines)",
				SessionID:     "a2a-platform-1",
				Worker:        "platform",
				DurationMs:    38,
				At:            now.Add(-40 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         2,
				Tool:          "kanban_create",
				Input:         `{"assignee":"cluster-diagnostics","title":"Live health check for drift-peer-ap-1"}`,
				CallID:        "c2",
				Status:        "ok",
				ResultPreview: "card_id=t_01 status=pending",
				SessionID:     "a2a-platform-1",
				Worker:        "platform",
				DurationMs:    19,
				At:            now.Add(-35 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         3,
				Tool:          "kanban_create",
				Input:         `{"assignee":"cluster-diagnostics","title":"Live health check for kube-agents-host"}`,
				CallID:        "c3",
				Status:        "ok",
				ResultPreview: "card_id=t_02 status=pending",
				SessionID:     "a2a-platform-1",
				Worker:        "platform",
				DurationMs:    21,
				At:            now.Add(-34 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         4,
				Tool:          "kanban_done",
				Input:         `{"summary":"Cluster RUNNING, 0 pod restarts","task_id":"t_01","verdict":"healthy"}`,
				CallID:        "c4",
				Status:        "ok",
				ResultPreview: "card_id=t_01 status=done",
				SessionID:     "worker-sess-01",
				Worker:        "card:t_01 (drift-peer-ap-1)",
				DurationMs:    15,
				At:            now.Add(-10 * time.Second).Format(time.RFC3339),
			},
			{
				Index:         5,
				Tool:          "kanban_heartbeat",
				Input:         `{"note":"Checking kubeagents-system pods on kube-agents-host","task_id":"t_02"}`,
				CallID:        "c5",
				Status:        "ok",
				ResultPreview: "heartbeat ok",
				SessionID:     "worker-sess-02",
				Worker:        "card:t_02 (kube-agents-host)",
				DurationMs:    12,
				At:            now.Add(-3 * time.Second).Format(time.RFC3339),
			},
		},
	}

	outcome := &TurnOutcome{
		ConversationAlias:     "default",
		ConversationToken:     "tok-dbg",
		PrimaryTask:           rec,
		TimedOut:              true,
		PrevStepCount:         4,
		ReportedStepCount:     4,
		PrevActivityCount:     4,
		ReportedActivityCount: 5,
	}

	// Verify A2: Consecutive-only coalescing keeps both non-consecutive "Reading skill..." steps (4 total steps, not 3).
	details := CollectOutcomeStepDetails(outcome)
	if len(details) != 4 {
		t.Fatalf("expected 4 steps with consecutive-only coalescing, got %d: %+v", len(details), details)
	}

	// Verify A1: When a poll times out with no new step (PrevStepCount == ReportedStepCount == 4),
	// it shows live Kanban card progress + delta activity (c5) and NEVER repeats c1 (`read_file`).
	waitTitle, waitMD, _ := FormatTurnCustomToolEnvelope(outcome, false)
	if !strings.Contains(waitTitle, "Checking kubeagents-system pods on kube-agents-host (1/2 cards done)") {
		t.Fatalf("unexpected waitTitle: %q", waitTitle)
	}
	if strings.Contains(waitMD, "read_file") {
		t.Fatalf("wait step markdown repeated stale read_file from earlier step:\n%s", waitMD)
	}
	if !strings.Contains(waitMD, "kanban_heartbeat") || !strings.Contains(waitMD, "Checking kubeagents-system pods on kube-agents-host") {
		t.Fatalf("wait step markdown missing delta kanban_heartbeat activity:\n%s", waitMD)
	}

	// Verify B1 + B2 + B3 in Debug Mode when task completes: all 4 collapsible panels render.
	rec.State = lib.StateCompleted
	rec.Terminal = true
	rec.ResultText = "Both clusters are healthy."
	outcome.TimedOut = false
	_, dbgMD, _ := FormatTurnCustomToolEnvelope(outcome, true)
	for _, needle := range []string{
		"Bus Envelope &amp; Task Telemetry",
		"Kanban Specialist Cards",
		"Full Tool Activity Stream",
		"Raw LLM API Turn Log",
		"card:t_01 (drift-peer-ap-1)",
		"# GKE Cluster Diagnostics Skill (112 lines)",
		"Both clusters are healthy.",
	} {
		if !strings.Contains(dbgMD, needle) {
			t.Errorf("debug mode output missing %q:\n%s", needle, dbgMD)
		}
	}
}

func TestOutOfOrderBootstrapReplayDoesNotCorruptCompletedTask(t *testing.T) {
	s := &BusSession{
		tasks:             make(map[string]*TaskRecord),
		seenEnvelopes:     make(map[string]bool),
		foregroundTasks:   make(map[string]time.Time),
		watchedTasks:      make(map[string]int),
		watchedConvs:      make(map[string]int),
		lastReportedSteps: make(map[string]int),
		bootstrapped:      true,
	}
	t0 := time.Date(2026, 10, 17, 14, 0, 30, 0, time.UTC)
	tEnd := t0.Add(514 * time.Second)
	taskID := "task-24c523717a80ef25"
	outSubj := "a2a.tasks.platform." + taskID + ".out"
	inSubj := "a2a.tasks.platform." + taskID + ".in"

	// 1. Live subscription receives the final completion envelopes FIRST (at tEnd = 14:09:04Z).
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-act-38", TS: tEnd.Add(-2 * time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: true,
			Artifact: lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"kanban_complete","input":{"id":"t_9ebe3ccb","result":"Scaled store-v1 to 2 replicas in PR #225"},"callId":"call-38","status":"completed","sessionId":"20261017_140036_e0c3f7","worker":"card:t_9ebe3ccb","durationMs":40,"at":"2026-10-17T14:09:02Z"}`)}}},
		}),
	}), true)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-think-end", TS: tEnd.Add(-time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: true,
			Artifact: lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Specialist check completed: Increase replica count on a workload (completed — 1/1 cards done).\tOpened PR #225 scaling store-v1 from 1 to 2 replicas."}}},
		}),
	}), true)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-res", TS: tEnd, TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: false,
			Artifact: lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "Increased replica count on `store-v1` from 1 to 2 via PR #225."}}},
		}),
	}), true)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-live-done", TS: tEnd, TaskID: taskID, ContextID: "ctx-1",
		Kind:    lib.KindStatusUpdate,
		Payload: mustJSON(lib.StatusUpdate{TaskID: taskID, Status: lib.TaskStatus{State: lib.StateCompleted}, Final: true}),
	}), true)

	// 2. Background stream bootstrap replays the OLDER start-of-task envelopes AFTER completion (at t0 = 14:00:30Z).
	s.ingestTaskEnvelope(inSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-msg", TS: t0, TaskID: taskID, ContextID: "ctx-1",
		Kind:    lib.KindMessage,
		Payload: mustJSON(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "increase replica count on a workload"}}}),
	}), false)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-working", TS: t0.Add(time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind:    lib.KindStatusUpdate,
		Payload: mustJSON(lib.StatusUpdate{TaskID: taskID, Status: lib.TaskStatus{State: lib.StateWorking}, Final: false}),
	}), false)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-think-1", TS: t0.Add(4 * time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: false,
			Artifact: lib.Artifact{Name: lib.ArtifactThinking, Parts: []lib.Part{{Kind: "text", Text: "Delegating specialist task: Increase replica count on a workload.\tCreating Kanban card."}}},
		}),
	}), false)
	s.ingestTaskEnvelope(outSubj, mustJSON(lib.Envelope{
		Protocol: lib.Protocol, EnvelopeID: "env-boot-act-1", TS: t0.Add(5 * time.Second), TaskID: taskID, ContextID: "ctx-1",
		Kind: lib.KindArtifactUpdate,
		Payload: mustJSON(lib.ArtifactUpdate{
			TaskID: taskID, Append: false,
			Artifact: lib.Artifact{Name: lib.ArtifactActivity, Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(`{"tool":"kanban_create","input":{"assignee":"platform","title":"Increase replica count on a workload"},"callId":"call-1","status":"completed","resultPreview":"Created task t_9ebe3ccb: Increase replica count on a workload","sessionId":"a2a-ctx-1","worker":"platform","durationMs":22,"at":"2026-10-17T14:00:35Z"}`)}}},
		}),
	}), false)

	rec := s.GetTask(taskID)
	if !rec.Terminal || rec.State != lib.StateCompleted {
		t.Fatalf("expected task to remain completed/terminal after out-of-order replay, got state=%s terminal=%v", rec.State, rec.Terminal)
	}
	if !strings.Contains(rec.Thinking, "Delegating specialist task") || !strings.Contains(rec.Thinking, "Specialist check completed") {
		t.Fatalf("expected both initial and final thinking steps preserved in chronological order, got:\n%s", rec.Thinking)
	}
	if len(rec.Activities) != 2 || rec.Activities[0].Tool != "kanban_create" || rec.Activities[1].Tool != "kanban_complete" {
		t.Fatalf("expected activities sorted chronologically [kanban_create, kanban_complete], got: %+v", rec.Activities)
	}
	cards, doneCount, runningCount, totalCount, _ := extractKanbanCardsFromTask(rec)
	if totalCount != 1 || doneCount != 1 || runningCount != 0 || cards[0].ID != "t_9ebe3ccb" {
		t.Fatalf("expected 1 completed card t_9ebe3ccb (no trailing colon duplicate), got total=%d done=%d running=%d cards=%+v",
			totalCount, doneCount, runningCount, cards)
	}
}

func TestDedupeNonAdjacentStepsAndBoundDebugOutput(t *testing.T) {
	// 1. Alternating steps A, B, A, B across >8 raw lines must collapse to A, B with merged thoughts.
	altRaw := strings.Join([]string{
		"Checking Kubernetes node status and readiness.\tChecking drift-peer-ap-1 nodes.",
		"Inspecting Kubernetes pods and workload health.\tChecking drift-peer-ap-1 pods.",
		"Checking Kubernetes node status and readiness.\tChecking drift-peer-ap-2 nodes.",
		"Inspecting Kubernetes pods and workload health.\tChecking drift-peer-ap-2 pods.",
		"Checking Kubernetes node status and readiness.\tRe-checking drift-peer-ap-1 nodepools.",
		"Inspecting Kubernetes pods and workload health.\tRe-checking drift-peer-ap-1 events.",
		"Checking Kubernetes node status and readiness.\tRe-checking drift-peer-ap-2 nodepools.",
		"Inspecting Kubernetes pods and workload health.\tRe-checking drift-peer-ap-2 events.",
		"Checking Kubernetes node status and readiness (Fleet-wide live status and node count check (16).",
	}, "\n")
	steps := parseAndCleanThinkingSteps(altRaw)
	if len(steps) != 2 {
		t.Fatalf("expected 2 deduplicated steps from alternating lines, got %d: %+v", len(steps), steps)
	}
	if !strings.Contains(steps[0].Thought, "drift-peer-ap-1") || !strings.Contains(steps[0].Thought, "drift-peer-ap-2") {
		t.Errorf("expected merged thoughts on step[0], got %q", steps[0].Thought)
	}

	// 2. Large 16-cluster fan-out (>40 raw thinking lines + 235 tool calls) must consolidate
	// generic cluster diagnostic lines while preserving all 16 Specialist check completed lines,
	// and FormatTaskRecordMarkdownWithOptions(..., true) must stay < 18 KB.
	var bigLines []string
	for i := 1; i <= 16; i++ {
		c := fmt.Sprintf("drift-peer-std-%d", i)
		bigLines = append(bigLines,
			fmt.Sprintf("Checking Kubernetes node status and readiness (%s).\tNodes check on %s.", c, c),
			fmt.Sprintf("Inspecting Kubernetes pods, nodes, and cluster events (%s).\tPods check on %s.", c, c),
			fmt.Sprintf("Inspecting files in the workspace (%s).\tWorkspace check on %s.", c, c),
			fmt.Sprintf("Specialist check completed: %s (done — %d/16 cards done).\tCluster %s is healthy.", c, i, c),
		)
	}
	var acts []ActivityItem
	for i := 1; i <= 235; i++ {
		acts = append(acts, ActivityItem{
			Index:         i,
			Tool:          "kubectl",
			Input:         fmt.Sprintf(`{"command":"kubectl get nodes -o wide --context=cluster-%d"}`, i),
			Status:        "completed",
			Worker:        fmt.Sprintf("card:t_%02d (drift-peer-std-%d)", (i%16)+1, (i%16)+1),
			SessionID:     fmt.Sprintf("20261017_sess_%02d", (i%16)+1),
			DurationMs:    340,
			At:            "2026-10-17T15:10:00Z",
			ResultPreview: "NAME STATUS ROLES AGE VERSION gke-node-1 Ready <none> 4d v1.33.5-gke.1100",
		})
	}
	rec := &TaskRecord{
		TaskID:            "task-big-fanout",
		ContextID:         "ctx-big",
		Conversation:      "console:soak",
		Addressee:         "platform",
		State:             lib.StateCompleted,
		Terminal:          true,
		Thinking:          strings.Join(bigLines, "\n"),
		Activities:        acts,
		IntermediateTurns: []string{strings.Repeat("Raw card report line. ", 600)},
		ResultText:        "All 16 clusters are checked.",
		EnvelopeCount:     310,
	}
	parsedBig := parseAndCleanThinkingSteps(rec.Thinking)
	if len(parsedBig) != 19 { // 3 consolidated diagnostic steps + 16 specialist completion steps
		t.Fatalf("expected 19 consolidated steps (3 diagnostic + 16 specialist completions), got %d", len(parsedBig))
	}
	debugMD := FormatTaskRecordMarkdownWithOptions(rec, "Primary Task", true)
	if len(debugMD) > 18500 {
		t.Fatalf("expected 16-cluster debug markdown <= 18.5 KB to prevent output.txt spill, got %d bytes", len(debugMD))
	}
}
