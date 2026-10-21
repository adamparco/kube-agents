package mcpbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestReparentOrphanedSessionSubagentAndSQLiteDB(t *testing.T) {
	const (
		deletedParentID = "176abbd9-c662-4ff9-86c8-246f35668b60"
		liveRootID      = "23d2408f-b48a-49a5-93dd-2a611830f2ba"
		subagentPoetID  = "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
	)

	rootDir := t.TempDir()
	brainDir := filepath.Join(rootDir, "brain")
	convDir := filepath.Join(rootDir, "conversations")
	if err := os.MkdirAll(brainDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(convDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// Only liveRootID and subagentPoetID exist in conversations/; deletedParentID was deleted.
	if err := os.WriteFile(filepath.Join(convDir, liveRootID+".db"), []byte("sqlite-root"), 0o644); err != nil {
		t.Fatal(err)
	}
	prefix := []byte("header-padding-before-metadata-")
	suffix := []byte("-trailing-sqlite-btree-payload")
	oldBlock := buildTrajectoryMetadataPatchBlock(deletedParentID)
	rawSubDB := append(append([]byte{}, prefix...), oldBlock...)
	rawSubDB = append(rawSubDB, suffix...)
	subDBPath := filepath.Join(convDir, subagentPoetID+".db")
	if err := os.WriteFile(subDBPath, rawSubDB, 0o644); err != nil {
		t.Fatal(err)
	}

	// Simulate stale subagent descriptor under deletedParentID and a split-brain swarm dir under subagentPoetID.
	staleDescDir := filepath.Join(brainDir, deletedParentID, ".system_generated", "subagents")
	if err := os.MkdirAll(staleDescDir, 0o755); err != nil {
		t.Fatal(err)
	}
	staleDescPath := filepath.Join(staleDescDir, subagentPoetID+".json")
	if err := os.WriteFile(staleDescPath, []byte(`{"spec":{"typeName":"kube-session","role":"Session Poet"}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	splitSwarmDir := filepath.Join(brainDir, subagentPoetID, ".system_generated", "swarm")
	if err := os.MkdirAll(splitSwarmDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(splitSwarmDir, "roster.json"), []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}

	if IsAGYConversationAlive(brainDir, deletedParentID) {
		t.Fatalf("expected deletedParentID %s to be reported as NOT alive", deletedParentID)
	}
	if !IsAGYConversationAlive(brainDir, liveRootID) {
		t.Fatalf("expected liveRootID %s to be reported as alive", liveRootID)
	}

	store, err := NewConfigStore(filepath.Join(rootDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	store.BindConversationWithLineage("default", ConversationLineage{
		ConversationID:     liveRootID,
		RootConversationID: liveRootID,
	})
	enabled := true
	_, _ = store.UpdatePushConfig(liveRootID, &enabled, "")
	if _, err := store.EnsureConversation("session-poet", false, "Poet session"); err != nil {
		t.Fatal(err)
	}
	store.BindConversationWithLineage("session-poet", ConversationLineage{
		ConversationID:       subagentPoetID,
		ParentConversationID: deletedParentID,
		RootConversationID:   deletedParentID,
		IsSubagent:           true,
		SubagentType:         subagentTypeKubeSession,
		SubagentRole:         "Session Poet",
	})

	resolver := NewAGYLineageResolver(brainDir)
	lin := ResolveLineageWithStore(resolver, store, subagentPoetID, "", liveRootID)
	if !lin.IsSubagent {
		t.Fatalf("expected subagentPoetID to resolve as subagent, got %+v", lin)
	}
	if lin.RootConversationID != liveRootID || lin.ParentConversationID != liveRootID {
		t.Fatalf("expected orphaned subagent to re-parent to %s, got %+v", liveRootID, lin)
	}
	if lin.SubagentRole != "Session Poet" {
		t.Fatalf("expected SubagentRole 'Session Poet', got %q", lin.SubagentRole)
	}

	// Verify new subagent descriptor was written under liveRootID and stale descriptor + split-brain swarm were removed.
	adoptedDescPath := filepath.Join(brainDir, liveRootID, ".system_generated", "subagents", subagentPoetID+".json")
	if _, err := os.Stat(adoptedDescPath); err != nil {
		t.Fatalf("expected adopted descriptor at %s: %v", adoptedDescPath, err)
	}
	if _, err := os.Stat(staleDescPath); !os.IsNotExist(err) {
		t.Fatalf("expected stale descriptor %s to be removed", staleDescPath)
	}
	if _, err := os.Stat(splitSwarmDir); !os.IsNotExist(err) {
		t.Fatalf("expected split-brain swarm dir %s to be removed", splitSwarmDir)
	}

	// Verify SQLite .db file was patched in-place with identical length and surrounding bytes.
	patchedDB, err := os.ReadFile(subDBPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(patchedDB) != len(rawSubDB) {
		t.Fatalf("expected in-place DB patch to preserve file length %d, got %d", len(rawSubDB), len(patchedDB))
	}
	wantBlock := buildTrajectoryMetadataPatchBlock(liveRootID)
	wantDB := append(append([]byte{}, prefix...), wantBlock...)
	wantDB = append(wantDB, suffix...)
	if !bytes.Equal(patchedDB, wantDB) {
		t.Fatalf("patched DB bytes mismatch:\ngot:  %x\nwant: %x", patchedDB, wantDB)
	}

	// Verify ConfigStore persisted the new ParentAGYConversationID.
	snap := store.Snapshot()
	if gotParent := snap.Conversations["session-poet"].ParentAGYConversationID; gotParent != liveRootID {
		t.Fatalf("ConfigStore session-poet ParentAGYConversationID = %q, want %q", gotParent, liveRootID)
	}
}

func TestReparentLiveAGYSummariesAndHookIsolation(t *testing.T) {
	const (
		deletedParentID = "176abbd9-c662-4ff9-86c8-246f35668b60"
		liveRootID      = "23d2408f-b48a-49a5-93dd-2a611830f2ba"
		subagentPoetID  = "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
		otherCodingID   = "9dd87ed9-09b7-48b9-a2ab-f5aa78525166"
	)

	var mu sync.Mutex
	writtenSummaries := make(map[string]map[string]any)
	mcpStatus := "MCP_SERVER_STATUS_ERROR"
	refreshCount := 0

	lsSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Header().Set("Content-Type", "text/html")
			_, _ = w.Write([]byte(`<html><script>window.__boot = {"csrfToken": "csrf-test-token-999"};</script></html>`))
		case rpcGetMcpServerStatesPath:
			mu.Lock()
			st := mcpStatus
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"states": []map[string]any{
					{
						"spec":   map[string]any{"serverName": "kube-agents"},
						"status": st,
					},
				},
			})
		case rpcRefreshMcpServersPath:
			mu.Lock()
			refreshCount++
			mcpStatus = "MCP_SERVER_STATUS_READY"
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{}`))
		case rpcGetAllTrajectoriesPath:
			if r.Header.Get("x-codeium-csrf-token") != "csrf-test-token-999" {
				http.Error(w, "invalid csrf", http.StatusForbidden)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"trajectorySummaries": map[string]any{
					subagentPoetID: map[string]any{
						"summary":   "Session Poet",
						"stepCount": 14,
						"status":    "CASCADE_RUN_STATUS_IDLE",
						"trajectoryMetadata": map[string]any{
							"parentConversationId": deletedParentID,
							"rootConversationId":   deletedParentID,
						},
					},
				},
			})
		case rpcWriteSummaryPath:
			if r.Header.Get("x-codeium-csrf-token") != "csrf-test-token-999" {
				http.Error(w, "invalid csrf", http.StatusForbidden)
				return
			}
			var req struct {
				CascadeID string         `json:"cascadeId"`
				Summary   map[string]any `json:"summary"`
			}
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			mu.Lock()
			writtenSummaries[req.CascadeID] = req.Summary
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer lsSrv.Close()

	statuses, err := ReparentLiveAGYSummaries(context.Background(), lsSrv.URL, liveRootID, map[string]string{
		subagentPoetID: "Session Poet",
	}, false)
	if err != nil {
		t.Fatalf("ReparentLiveAGYSummaries: %v", err)
	}
	if statuses[subagentPoetID] != "CASCADE_RUN_STATUS_IDLE" {
		t.Fatalf("expected status CASCADE_RUN_STATUS_IDLE, got %q", statuses[subagentPoetID])
	}

	mu.Lock()
	written := writtenSummaries[subagentPoetID]
	mu.Unlock()
	if written == nil {
		t.Fatalf("expected the trajectory summary write to be called for %s", subagentPoetID)
	}
	meta, _ := written["trajectoryMetadata"].(map[string]any)
	if meta["parentConversationId"] != liveRootID || meta["rootConversationId"] != liveRootID {
		t.Fatalf("expected parentConversationId & rootConversationId = %s, got %+v", liveRootID, meta)
	}
	if written["summary"] != "Session Poet" {
		t.Fatalf("expected summary title 'Session Poet' to be preserved, got %v", written["summary"])
	}

	// Verify HandleAGYHook does NOT let a subagent or an unrelated known conversation overwrite Push.AGYConversationID.
	tmpDir := t.TempDir()
	store, err := NewConfigStore(filepath.Join(tmpDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	enabled := true
	_, _ = store.UpdatePushConfig(liveRootID, &enabled, "")
	_, _ = store.UpdatePushConfig(otherCodingID, &enabled, "")
	_, _ = store.UpdatePushConfig(liveRootID, &enabled, "")
	store.BindConversationWithLineage("default", ConversationLineage{
		ConversationID:     liveRootID,
		RootConversationID: liveRootID,
	})
	if _, err := store.EnsureConversation("session-poet", false, ""); err != nil {
		t.Fatal(err)
	}
	store.BindConversationWithLineage("session-poet", ConversationLineage{
		ConversationID:       subagentPoetID,
		ParentConversationID: liveRootID,
		RootConversationID:   liveRootID,
		IsSubagent:           true,
		SubagentType:         subagentTypeKubeSession,
		SubagentRole:         "Session Poet",
	})

	var hookOut bytes.Buffer
	t.Setenv(envAGYLanguageServerURL, lsSrv.URL)
	if err := HandleAGYHook(store, strings.NewReader(`{"conversationId":"`+subagentPoetID+`"}`), &hookOut); err != nil {
		t.Fatal(err)
	}
	if got := store.ResolveAGYConversationID(); got != liveRootID {
		t.Fatalf("subagent hook overwrote active root conversation ID: got %q, want %q", got, liveRootID)
	}
	mu.Lock()
	gotRefreshes := refreshCount
	mu.Unlock()
	if gotRefreshes != 1 {
		t.Fatalf("expected HandleAGYHook to trigger RefreshMcpServers once when status is ERROR, got %d", gotRefreshes)
	}

	hookOut.Reset()
	if err := HandleAGYHook(store, strings.NewReader(`{"conversationId":"`+otherCodingID+`"}`), &hookOut); err != nil {
		t.Fatal(err)
	}
	if got := store.ResolveAGYConversationID(); got != liveRootID {
		t.Fatalf("non-active conversation hook overwrote active root conversation ID: got %q, want %q", got, liveRootID)
	}
}

func TestPruneStaleKubeAgentsSwarmsOnSync(t *testing.T) {
	const (
		deadEarlierCoordID = "11b0c9bb-be67-498c-bed2-17781529a1e3"
		liveCoordinatorID  = "513ff2a8-181d-456f-88a4-e311230949e8"
		otherLiveCodingID  = "9dd87ed9-09b7-48b9-a2ab-f5aa78525166"
		subagentPoetID     = "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
	)

	rootDir := t.TempDir()
	brainDir := filepath.Join(rootDir, "brain")
	convDir := filepath.Join(rootDir, "conversations")
	if err := os.MkdirAll(brainDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(convDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// Only liveCoordinatorID, otherLiveCodingID, and subagentPoetID exist in conversations/.
	for _, liveID := range []string{liveCoordinatorID, otherLiveCodingID, subagentPoetID} {
		if err := os.WriteFile(filepath.Join(convDir, liveID+".db"), []byte("sqlite"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	writeStaleSwarm := func(convID string) string {
		sDir := filepath.Join(brainDir, convID, ".system_generated", "swarm")
		if err := os.MkdirAll(sDir, 0o755); err != nil {
			t.Fatal(err)
		}
		roster := SwarmRosterFile{
			Title: "kube-agents swarm",
			Members: map[string]SwarmMember{
				"coordinator": {
					Name:           "coordinator",
					Role:           "coordinator",
					ConversationID: convID,
					ManagedBy:      swarmManagedByTag,
				},
				"session-poet": {
					Name:           "session-poet",
					Role:           "worker",
					ConversationID: subagentPoetID,
					ManagedBy:      swarmManagedByTag,
				},
			},
		}
		raw, _ := json.MarshalIndent(roster, "", "  ")
		if err := os.WriteFile(filepath.Join(sDir, swarmMembersFileName), raw, 0o644); err != nil {
			t.Fatal(err)
		}
		return sDir
	}

	deadSwarmDir := writeStaleSwarm(deadEarlierCoordID)
	otherSwarmDir := writeStaleSwarm(otherLiveCodingID)

	sink := NewAGYSwarmSink()

	// 1. Attempting to sync a dead conversation ID must be a no-op.
	if err := sink.Sync(context.Background(), SwarmProjectionInput{
		RootConversationID: "00000000-0000-0000-0000-000000000000",
		BrainDir:           brainDir,
		Connected:          true,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(brainDir, "00000000-0000-0000-0000-000000000000")); !os.IsNotExist(err) {
		t.Fatalf("expected Sync on dead conversation ID not to create brain dir")
	}

	// 2. Syncing liveCoordinatorID must prune both deadEarlierCoordID and otherLiveCodingID's kube-agents swarm dirs.
	if err := sink.Sync(context.Background(), SwarmProjectionInput{
		RootConversationID: liveCoordinatorID,
		BrainDir:           brainDir,
		Deployment: DeploymentProfile{
			Name:              "kube-agents-host",
			Namespace:         "kubeagents-system",
			PlatformAgentName: "platform",
		},
		Connected: true,
		Conversations: map[string]ConversationEntry{
			"session-poet": {
				Alias:                   "session-poet",
				Token:                   "agy-poet",
				SessionMode:             true,
				AGYConversationID:       subagentPoetID,
				ParentAGYConversationID: liveCoordinatorID,
				SubagentRole:            "Session Poet",
			},
		},
	}); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(deadSwarmDir); !os.IsNotExist(err) {
		t.Fatalf("expected dead coordinator swarm dir %s to be pruned", deadSwarmDir)
	}
	if _, err := os.Stat(filepath.Join(brainDir, deadEarlierCoordID)); !os.IsNotExist(err) {
		t.Fatalf("expected empty dead coordinator brain dir %s to be removed", deadEarlierCoordID)
	}
	if _, err := os.Stat(otherSwarmDir); !os.IsNotExist(err) {
		t.Fatalf("expected stale kube-agents swarm dir on other conversation %s to be pruned", otherSwarmDir)
	}
	if _, err := os.Stat(filepath.Join(brainDir, otherLiveCodingID)); err != nil {
		t.Fatalf("expected live conversation brain dir %s itself to remain intact: %v", otherLiveCodingID, err)
	}

	liveMembersPath := filepath.Join(brainDir, liveCoordinatorID, ".system_generated", "swarm", swarmMembersFileName)
	rawLive, err := os.ReadFile(liveMembersPath)
	if err != nil {
		t.Fatalf("expected live coordinator members.json at %s: %v", liveMembersPath, err)
	}
	var liveRoster SwarmRosterFile
	if err := json.Unmarshal(rawLive, &liveRoster); err != nil {
		t.Fatal(err)
	}
	if got := liveRoster.Members["coordinator"].ConversationID; got != liveCoordinatorID {
		t.Fatalf("live coordinator conversation_id = %q, want %q", got, liveCoordinatorID)
	}
	if got := liveRoster.Members["session-poet"].ConversationID; got != subagentPoetID {
		t.Fatalf("session-poet conversation_id = %q, want %q", got, subagentPoetID)
	}
}

func TestMultiConversationCoordinatorIsolationAndGarbageCollection(t *testing.T) {
	const (
		liveCoordinatorID = "513ff2a8-181d-456f-88a4-e311230949e8"
		otherCodingID     = "9dd87ed9-09b7-48b9-a2ab-f5aa78525166"
		otherSubagentID   = "663c1045-d1e1-4bf9-ab48-86d40d284375"
		subagentPoetID    = "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
		deadAGYID         = "deadbeef-0000-4000-8000-000000000001"
	)

	rootDir := t.TempDir()
	brainDir := filepath.Join(rootDir, "brain")
	convDir := filepath.Join(rootDir, "conversations")
	if err := os.MkdirAll(brainDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(convDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, liveID := range []string{liveCoordinatorID, otherCodingID, otherSubagentID, subagentPoetID} {
		if err := os.WriteFile(filepath.Join(convDir, liveID+".db"), []byte("sqlite"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Join(brainDir, liveID), 0o755); err != nil {
			t.Fatal(err)
		}
	}

	// Register otherSubagentID under otherCodingID.
	otherSubDir := filepath.Join(brainDir, otherCodingID, ".system_generated", "subagents")
	if err := os.MkdirAll(otherSubDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(otherSubDir, otherSubagentID+".json"), []byte(`{"conversationId":"`+otherSubagentID+`","subagentDescriptor":{"typeName":"kube-agents","role":"Soak Tester"}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	gwSrv := startMockGatewayMCPServer(t, nil)

	store, err := NewConfigStore(filepath.Join(rootDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{
		Store:   store,
		Lineage: NewAGYLineageResolver(brainDir),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	// 1. Live coordinator connects to deployment and owns session-poet.
	coordConnectReq, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "tools/call",
		"params": map[string]any{
			"name": toolConnectDeployment,
			"arguments": map[string]any{
				"deployment":      testClusterName,
				"namespace":       testNamespace,
				"direct_nats_url": gwSrv.URL,
			},
			"_meta": map[string]any{
				"antigravity.google/conversation_id": liveCoordinatorID,
			},
		},
	})
	_ = mcpSrv.HandleJSONRPC(context.Background(), coordConnectReq)

	// Subagent poet initializes its /session conversation under liveCoordinatorID.
	if _, err := store.EnsureConversationWithOptions("session-poet", false, "Poet", false); err != nil {
		t.Fatal(err)
	}
	_, _ = store.SetConversationSessionMode("session-poet", true)
	store.BindConversationWithLineage("session-poet", ConversationLineage{
		ConversationID:       subagentPoetID,
		ParentConversationID: liveCoordinatorID,
		RootConversationID:   liveCoordinatorID,
		IsSubagent:           true,
		SubagentType:         subagentTypeKubeSession,
		SubagentRole:         "Session Poet",
	})

	// Seed a dead conversation, a user-named conversation that merely looks like a minted shell,
	// and a server-minted shell for an Antigravity conversation that has since been deleted.
	_, _ = store.EnsureConversationWithOptions("agy-dead0001", true, "Dead shell", false)
	store.BindConversationAGYID("agy-dead0001", deadAGYID)
	_, _ = store.EnsureConversationWithOptions("agy-unbound1", true, "User-named conversation", false)
	mintedShell := store.ConversationForAGYWithOptions(ConversationLineage{
		ConversationID:     "deadbeef-0000-4000-8000-000000000002",
		RootConversationID: "deadbeef-0000-4000-8000-000000000002",
	}, false)
	if !mintedShell.AutoBound {
		t.Fatalf("server-minted conversation must be marked AutoBound, got %+v", mintedShell)
	}
	// Restore coordinator push config after seeding dead test entries.
	enabled := true
	_, _ = store.UpdatePushConfig(liveCoordinatorID, &enabled, "")

	coordAlias := store.ActiveConversation().Alias

	// 2. Call passive & explicit-conversation tools from otherCodingID and otherSubagentID.
	passiveCalls := []struct {
		callerID string
		tool     string
		args     map[string]any
	}{
		{otherCodingID, toolConnectionStatus, map[string]any{}},
		{otherCodingID, toolReadTopics, map[string]any{"action": "list"}},
		{otherCodingID, toolManageSession, map[string]any{"action": "list"}},
		{otherCodingID, toolManageSession, map[string]any{"action": "transcript", "alias": "session-poet"}},
		{otherCodingID, toolTaskControl, map[string]any{"action": "list"}},
		{otherCodingID, toolPollEvents, map[string]any{"peek_only": true}},
		{otherCodingID, toolManageSession, map[string]any{"action": "new", "alias": "soak-isolated"}},
		{otherCodingID, toolSendMessage, map[string]any{"conversation": "soak-isolated", "message": "Check upgrade readiness", "wait_timeout_seconds": 10}},
		{otherSubagentID, toolManageSession, map[string]any{"action": "list"}},
	}
	for idx, tc := range passiveCalls {
		rawReq, _ := json.Marshal(map[string]any{
			"jsonrpc": "2.0",
			"id":      100 + idx,
			"method":  "tools/call",
			"params": map[string]any{
				"name":      tc.tool,
				"arguments": tc.args,
				"_meta": map[string]any{
					"antigravity.google/conversation_id": tc.callerID,
				},
			},
		})
		_ = mcpSrv.HandleJSONRPC(context.Background(), rawReq)
	}

	// 3. Verify liveCoordinatorID is still the active root coordinator, ActiveConversation is unchanged,
	// session-poet is still parented to liveCoordinatorID, and otherCodingID has NO swarm dir.
	if gotPush := store.ResolveAGYConversationID(); gotPush != liveCoordinatorID {
		t.Fatalf("expected Push.AGYConversationID to remain %s, got %q", liveCoordinatorID, gotPush)
	}
	if gotActive := store.ActiveConversation().Alias; gotActive != coordAlias {
		t.Fatalf("expected ActiveConversation to remain %q, got %q", coordAlias, gotActive)
	}
	snap := store.Snapshot()
	if gotParent := snap.Conversations["session-poet"].ParentAGYConversationID; gotParent != liveCoordinatorID {
		t.Fatalf("expected session-poet parent to remain %s, got %q", liveCoordinatorID, gotParent)
	}
	if gotSubID := snap.Conversations["session-poet"].AGYConversationID; gotSubID != subagentPoetID {
		t.Fatalf("expected session-poet AGYConversationID to remain %s, got %q", subagentPoetID, gotSubID)
	}
	if _, hasDead := snap.Conversations["agy-dead0001"]; hasDead {
		t.Fatalf("expected PruneStaleConversations to remove dead conversation agy-dead0001")
	}
	if _, hasMinted := snap.Conversations[mintedShell.Alias]; hasMinted {
		t.Fatalf("expected PruneStaleConversations to remove the minted shell %q of a deleted Antigravity conversation", mintedShell.Alias)
	}
	if _, hasUserNamed := snap.Conversations["agy-unbound1"]; !hasUserNamed {
		t.Fatalf("a fresh user-named conversation must never be pruned because of how its alias is spelled")
	}
	if _, err := os.Stat(filepath.Join(brainDir, otherCodingID, ".system_generated", "swarm")); !os.IsNotExist(err) {
		t.Fatalf("expected otherCodingID NOT to have a .system_generated/swarm directory")
	}
	if _, err := os.Stat(filepath.Join(brainDir, liveCoordinatorID, ".system_generated", "swarm", swarmMembersFileName)); err != nil {
		t.Fatalf("expected liveCoordinatorID swarm members.json to exist: %v", err)
	}
}

func TestKeepAliveFailbackOnDeletedCoordinatorAndColdTrajectoryLoad(t *testing.T) {
	const (
		primaryCoordID = "ed17511a-eb6b-4ba0-8f98-6c3421212ca2"
		tempCoordID    = "0f92c26d-9a17-4fc9-ba5d-a8c7295637b6"
		subagentPoetID = "f017a8f2-234c-4471-bb8e-30c30ace1dbf"
	)

	rootDir := t.TempDir()
	brainDir := filepath.Join(rootDir, "brain")
	convDir := filepath.Join(rootDir, "conversations")
	if err := os.MkdirAll(brainDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(convDir, 0o755); err != nil {
		t.Fatal(err)
	}

	for _, id := range []string{primaryCoordID, tempCoordID} {
		if err := os.WriteFile(filepath.Join(convDir, id+".db"), []byte("sqlite"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll(filepath.Join(brainDir, id), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	subDBPath := filepath.Join(convDir, subagentPoetID+".db")
	rawSubDB := append([]byte("hdr-"), buildTrajectoryMetadataPatchBlock(primaryCoordID)...)
	if err := os.WriteFile(subDBPath, rawSubDB, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(brainDir, subagentPoetID), 0o755); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	coldLoaded := make(map[string]bool)
	writtenSummaries := make(map[string]map[string]any)

	lsSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.Header().Set("Content-Type", "text/html")
			_, _ = w.Write([]byte(`<html><script>window.__boot = {"csrfToken": "csrf-keepalive-test"};</script></html>`))
		case rpcLoadTrajectoryPath:
			var req struct {
				CascadeID string `json:"cascadeId"`
			}
			_ = json.NewDecoder(r.Body).Decode(&req)
			mu.Lock()
			coldLoaded[req.CascadeID] = true
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{}`))
		case rpcGetAllTrajectoriesPath:
			mu.Lock()
			loaded := coldLoaded[subagentPoetID]
			mu.Unlock()
			summaries := map[string]any{}
			if loaded {
				summaries[subagentPoetID] = map[string]any{
					"summary":   "Session Poet",
					"stepCount": 14,
					"status":    "CASCADE_RUN_STATUS_IDLE",
					"trajectoryMetadata": map[string]any{
						"parentConversationId": tempCoordID,
						"rootConversationId":   tempCoordID,
					},
				}
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"trajectorySummaries": summaries,
			})
		case rpcWriteSummaryPath:
			var req struct {
				CascadeID string         `json:"cascadeId"`
				Summary   map[string]any `json:"summary"`
			}
			_ = json.NewDecoder(r.Body).Decode(&req)
			mu.Lock()
			writtenSummaries[req.CascadeID] = req.Summary
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer lsSrv.Close()

	gwSrv := startMockGatewayMCPServer(t, nil)

	store, err := NewConfigStore(filepath.Join(rootDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{
		Store:     store,
		Lineage:   NewAGYLineageResolver(brainDir),
		LSBaseURL: lsSrv.URL,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	// 1. Primary coordinator ("kube-agents", ed17511a) connects and spawns session-poet.
	connectPrimary, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "tools/call",
		"params": map[string]any{
			"name": toolConnectDeployment,
			"arguments": map[string]any{
				"deployment":      testClusterName,
				"namespace":       testNamespace,
				"direct_nats_url": gwSrv.URL,
			},
			"_meta": map[string]any{
				"antigravity.google/conversation_id": primaryCoordID,
			},
		},
	})
	_ = mcpSrv.HandleJSONRPC(context.Background(), connectPrimary)

	if _, err := store.EnsureConversationWithOptions("session-poet", false, "Poet", false); err != nil {
		t.Fatal(err)
	}
	_, _ = store.SetConversationSessionMode("session-poet", true)
	store.BindConversationWithLineage("session-poet", ConversationLineage{
		ConversationID:       subagentPoetID,
		ParentConversationID: primaryCoordID,
		RootConversationID:   primaryCoordID,
		IsSubagent:           true,
		SubagentType:         subagentTypeKubeSession,
		SubagentRole:         "Session Poet",
	})

	// 2. Temporary test conversation (0f92c26d) connects and claims the swarm.
	connectTemp, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      2,
		"method":  "tools/call",
		"params": map[string]any{
			"name": toolConnectDeployment,
			"arguments": map[string]any{
				"deployment":      testClusterName,
				"namespace":       testNamespace,
				"direct_nats_url": gwSrv.URL,
			},
			"_meta": map[string]any{
				"antigravity.google/conversation_id": tempCoordID,
			},
		},
	})
	_ = mcpSrv.HandleJSONRPC(context.Background(), connectTemp)

	if got := store.ResolveAGYConversationID(); got != tempCoordID {
		t.Fatalf("expected tempCoordID %s to claim Push.AGYConversationID, got %q", tempCoordID, got)
	}
	if _, err := os.Stat(filepath.Join(brainDir, primaryCoordID, ".system_generated", "swarm")); !os.IsNotExist(err) {
		t.Fatalf("expected primaryCoordID swarm dir to be pruned while tempCoordID owned the swarm")
	}

	// 3. Simulate MCP server restart (session = nil) + user deleting tempCoordID while primaryCoordID sits idle.
	mcpSrv.mu.Lock()
	if mcpSrv.session != nil {
		mcpSrv.session.Close()
		mcpSrv.session = nil
	}
	mcpSrv.lastConvPruneAt = time.Time{}
	mcpSrv.lastSummaryVerifyAt = time.Time{}
	mcpSrv.mu.Unlock()
	mu.Lock()
	delete(coldLoaded, subagentPoetID)
	delete(writtenSummaries, subagentPoetID)
	mu.Unlock()

	_ = os.Remove(filepath.Join(convDir, tempCoordID+".db"))
	_ = os.RemoveAll(filepath.Join(brainDir, tempCoordID))

	// 4. Run keepAliveTick (no user chat message sent!).
	mcpSrv.keepAliveTick(context.Background())

	// Wait briefly for ensureConnectedAsync to finish reconnecting and projecting.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		mcpSrv.mu.Lock()
		healthy := mcpSrv.session != nil && mcpSrv.session.IsHealthy()
		mcpSrv.mu.Unlock()
		if healthy {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	mcpSrv.keepAliveTick(context.Background())

	if got := store.ResolveAGYConversationID(); got != primaryCoordID {
		t.Fatalf("expected keepAliveTick to fail back Push.AGYConversationID to primaryCoordID %s, got %q", primaryCoordID, got)
	}
	snap := store.Snapshot()
	if gotParent := snap.Conversations["session-poet"].ParentAGYConversationID; gotParent != primaryCoordID {
		t.Fatalf("expected session-poet to be re-parented to primaryCoordID %s, got %q", primaryCoordID, gotParent)
	}
	if _, err := os.Stat(filepath.Join(brainDir, primaryCoordID, ".system_generated", "swarm", swarmMembersFileName)); err != nil {
		t.Fatalf("expected keepAliveTick to restore primaryCoordID swarm members.json without user message: %v", err)
	}

	mu.Lock()
	wasLoaded := coldLoaded[subagentPoetID]
	written := writtenSummaries[subagentPoetID]
	mu.Unlock()
	if !wasLoaded {
		t.Fatalf("expected ReparentLiveAGYSummaries to pre-warm cold subagent %s via LoadTrajectory", subagentPoetID)
	}
	if written == nil {
		t.Fatalf("expected the trajectory summary write to be called for cold-loaded subagent %s", subagentPoetID)
	}
	meta, _ := written["trajectoryMetadata"].(map[string]any)
	if meta["parentConversationId"] != primaryCoordID {
		t.Fatalf("expected trajectory summary parentConversationId = %s, got %+v", primaryCoordID, meta)
	}
}

func TestScanLiveMCPServerProcessAndVersionCLI(t *testing.T) {
	if err := RunCLI([]string{"--version"}, nil); err != nil {
		t.Fatalf("RunCLI(--version) failed: %v", err)
	}
	if err := RunCLI([]string{"version"}, nil); err != nil {
		t.Fatalf("RunCLI(version) failed: %v", err)
	}

	root := t.TempDir()
	imagesDir := filepath.Join(root, "images")
	oldImg := filepath.Join(imagesDir, "image-old", "kube-agents-mcp")
	newImg := filepath.Join(imagesDir, "image-new", "kube-agents-mcp")
	if err := os.MkdirAll(filepath.Dir(oldImg), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(newImg), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(oldImg, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(newImg, []byte("new"), 0o755); err != nil {
		t.Fatal(err)
	}
	releaseLink := filepath.Join(root, "releases", "kube-agents-mcp")
	if err := os.MkdirAll(filepath.Dir(releaseLink), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(newImg, releaseLink); err != nil {
		t.Fatal(err)
	}

	// Case 1: release symlink switched from oldImg to newImg; running PID 4242 still has /proc/4242/exe -> oldImg.
	procRoot := filepath.Join(root, "proc1")
	pidDir := filepath.Join(procRoot, "4242")
	if err := os.MkdirAll(pidDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(oldImg, filepath.Join(pidDir, "exe")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(pidDir, "cmdline"), []byte(releaseLink+"\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
	var killed []int
	alive := scanLiveMCPServerProcess(procRoot, newImg, "9999", func(pid int) {
		killed = append(killed, pid)
	})
	if alive {
		t.Fatalf("expected scanLiveMCPServerProcess to return false when only a stale release-image process is running")
	}
	if len(killed) != 1 || killed[0] != 4242 {
		t.Fatalf("expected stale PID 4242 to be killed, got %v", killed)
	}

	// Case 2: Up-to-date server process (exe -> newImg).
	procRoot2 := filepath.Join(root, "proc2")
	pidDir2 := filepath.Join(procRoot2, "5353")
	if err := os.MkdirAll(pidDir2, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(newImg, filepath.Join(pidDir2, "exe")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(pidDir2, "cmdline"), []byte(releaseLink+"\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
	killed = nil
	if !scanLiveMCPServerProcess(procRoot2, newImg, "9999", func(pid int) { killed = append(killed, pid) }) {
		t.Fatalf("expected scanLiveMCPServerProcess to return true when up-to-date server is running")
	}
	if len(killed) != 0 {
		t.Fatalf("expected no processes killed when server is up-to-date, got %v", killed)
	}
}

func TestSwarmOverviewLayoutConnectAndCancelPropagation(t *testing.T) {
	const liveCoordID = "75cf6a0d-31ff-40c9-8bc7-6938e6fc761c"
	rootDir := t.TempDir()
	brainDir := filepath.Join(rootDir, "brain")
	convDir := filepath.Join(rootDir, "conversations")
	if err := os.MkdirAll(brainDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(convDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(convDir, liveCoordID+".db"), []byte("sqlite"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Verify Version & Upgrade Overview is rendered in the middle (before Deployment & A2A JetStream Status)
	// and never references kube-agents-cli.
	html := buildSwarmOverviewHTML(&KubeAgentsOverviewState{
		Connected:          true,
		RootConversationID: liveCoordID,
		ClusterName:        "kube-agents-host",
		Namespace:          "kubeagents-system",
		PlatformAgentName:  "platform-agent",
		InstalledVersion:   "v0.15.0",
		LatestReleaseTag:   "v0.16.0",
		UpgradeAvailable:   true,
	})

	versionIdx := strings.Index(html, "Version &amp; Upgrade Overview")
	deployIdx := strings.Index(html, "Deployment &amp; A2A JetStream Status")
	if versionIdx == -1 || deployIdx == -1 {
		t.Fatalf("expected both Version and Deployment sections in connected overview HTML")
	}
	if versionIdx >= deployIdx {
		t.Fatalf("expected Version & Upgrade Overview (idx %d) to appear BEFORE Deployment & A2A JetStream Status (idx %d)", versionIdx, deployIdx)
	}
	if strings.Contains(html, "kube-agents-cli") {
		t.Fatalf("expected overview HTML not to reference nonexistent kube-agents-cli")
	}

	// Verify user cancellation detection from transcript.jsonl ("User cancelled agent execution.")
	// and propagation to remote task_control(stop).
	logDir := filepath.Join(brainDir, liveCoordID, ".system_generated", "logs")
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		t.Fatal(err)
	}
	transcriptPath := filepath.Join(logDir, "transcript.jsonl")
	if err := os.WriteFile(transcriptPath, []byte(`{"step_index":10,"type":"PLANNER_RESPONSE","status":"CANCELED","content":"User cancelled agent execution."}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !IsAGYCascadeUserCanceled(context.Background(), "", brainDir, liveCoordID) {
		t.Fatalf("expected IsAGYCascadeUserCanceled to detect 'User cancelled agent execution.' in transcript.jsonl")
	}

	var mu sync.Mutex
	var stopCalledForTask string
	gwSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case remoteMCPHealthPath:
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"ok","name":"kube-agents","version":"0.4.0"}`))
		case remoteMCPSwarmPath:
			var swarmReq RemoteSwarmRequest
			_ = json.NewDecoder(r.Body).Decode(&swarmReq)
			snap, nextLedger := buildLocalSwarmState(SwarmProjectionInput{
				RootConversationID: swarmReq.RootConversationID,
				Deployment:         swarmReq.Deployment,
				Connected:          true,
				Conversations:      swarmReq.Conversations,
				Subagents:          swarmReq.Subagents,
				Now:                time.Now().UTC(),
			}, swarmReq.Ledger)
			w.Header().Set("Content-Type", contentTypeJSON)
			_ = json.NewEncoder(w).Encode(RemoteSwarmResponse{
				Connected:          true,
				Deployment:         swarmReq.Deployment,
				Snapshot:           snap,
				Ledger:             nextLedger,
				Conversations:      swarmReq.Conversations,
				ActiveConversation: swarmReq.ActiveConversation,
			})
		default:
			var req rpcRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			if req.Method == methodToolsCall {
				var params struct {
					Name      string         `json:"name"`
					Arguments map[string]any `json:"arguments"`
				}
				_ = json.Unmarshal(req.Params, &params)
				if params.Name == toolTaskControl {
					act, _ := params.Arguments["action"].(string)
					if strings.EqualFold(act, "stop") {
						tid, _ := params.Arguments["task_id"].(string)
						mu.Lock()
						stopCalledForTask = tid
						mu.Unlock()
					}
				}
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(marshalRPCResult(req.ID, CallToolResult{
				Content: []TextContent{{Type: contentTypeText, Text: "ok"}},
			}))
		}
	}))
	defer gwSrv.Close()

	store, err := NewConfigStore(filepath.Join(rootDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	_ = store.UpsertDeployments([]DeploymentProfile{{
		Name:              testClusterName,
		Namespace:         testNamespace,
		PlatformAgentName: "platform-agent",
		DirectNATSURL:     gwSrv.URL,
		Ready:             true,
	}})
	_, _ = store.SetActiveDeployment(testClusterName)
	mcpSrv, err := NewServer(ServerOptions{
		Store:   store,
		Lineage: NewAGYLineageResolver(brainDir),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	// Connect mcpSrv so it has a live session to gwSrv.
	connectReq, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      1,
		"method":  "tools/call",
		"params": map[string]any{
			"name": toolConnectDeployment,
			"arguments": map[string]any{
				"deployment":      testClusterName,
				"namespace":       testNamespace,
				"direct_nats_url": gwSrv.URL,
			},
			"_meta": map[string]any{
				"antigravity.google/conversation_id": liveCoordID,
			},
		},
	})
	_ = mcpSrv.HandleJSONRPC(context.Background(), connectReq)

	// Simulate an active streaming task waiting between STREAMING_STEP_CONTINUE calls when user clicks Stop.
	mcpSrv.registerStreamingCallStart("default", "task-cancel-999", "default", liveCoordID, "", nil)
	mcpSrv.finishStreamingCall("default", "default", liveCoordID, "", "STREAMING_STEP_CONTINUE: Task task-cancel-999 is actively running...", nil, nil)
	mcpSrv.checkActiveStreamingTasksForCancel(context.Background())

	mu.Lock()
	gotStopped := stopCalledForTask
	mu.Unlock()
	if gotStopped != "task-cancel-999" {
		t.Fatalf("expected checkActiveStreamingTasksForCancel to stop task-cancel-999, got %q", gotStopped)
	}
}
