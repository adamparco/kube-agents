package mcpbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	envAGYLanguageServerURL   = "KUBE_AGENTS_AGY_LS_URL"
	envAntigravityCSRFToken   = "ANTIGRAVITY_CSRF_TOKEN"
	defaultAGYLSBaseURL       = "http://127.0.0.1:5387"
	rpcGetAllTrajectoriesPath = "/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
	rpcLoadTrajectoryPath     = "/exa.language_server_pb.LanguageServerService/LoadTrajectory"
	rpcDeleteTrajectoryPath   = "/exa.language_server_pb.LanguageServerService/DeleteCascadeTrajectory"
	rpcWriteSummaryPath       = "/exa.language_server_pb.LanguageServerService/JetboxWriteSummary"
	rpcGetMcpServerStatesPath = "/exa.language_server_pb.LanguageServerService/GetMcpServerStates"
	rpcRefreshMcpServersPath  = "/exa.language_server_pb.LanguageServerService/RefreshMcpServers"
	lsHTTPTimeout             = 8 * time.Second
	uuidStringLen             = 36
	metadataPatchBlockLen     = 76 // \x2a\x24 + 36-byte UUID + \x32\x24 + 36-byte UUID
)

var csrfTokenHTMLRe = regexp.MustCompile(`"csrfToken"\s*:\s*"([^"]+)"`)

// isCanonicalUUID reports whether s is a 36-byte lowercase ASCII UUID (8-4-4-4-12 hex).
func isCanonicalUUID(s string) bool {
	if len(s) != uuidStringLen {
		return false
	}
	return isCanonicalUUIDBytes([]byte(s))
}

func isCanonicalUUIDBytes(b []byte) bool {
	if len(b) != uuidStringLen {
		return false
	}
	for i, c := range b {
		switch i {
		case 8, 13, 18, 23:
			if c != '-' {
				return false
			}
		default:
			isHex := (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
			if !isHex {
				return false
			}
		}
	}
	return true
}

// humanizeSubagentRole converts a session alias like "session-cluster-explorer"
// into a readable title like "Session Cluster Explorer".
func humanizeSubagentRole(alias string) string {
	parts := strings.FieldsFunc(strings.TrimSpace(alias), func(r rune) bool {
		return r == '-' || r == '_' || r == ' '
	})
	if len(parts) == 0 {
		return "Session Worker"
	}
	for i, p := range parts {
		if len(p) > 0 {
			parts[i] = strings.ToUpper(p[:1]) + strings.ToLower(p[1:])
		}
	}
	return strings.Join(parts, " ")
}

// isSessionSubagentEntry reports whether a persisted ConversationEntry represents a
// dedicated /session subagent rather than a root coordinator conversation.
func isSessionSubagentEntry(c ConversationEntry) bool {
	if strings.TrimSpace(c.ParentAGYConversationID) != "" &&
		strings.TrimSpace(c.ParentAGYConversationID) != strings.TrimSpace(c.AGYConversationID) {
		return true
	}
	// Only structural lineage (a distinct parent conversation or a recorded subagent role)
	// classifies an entry as a subagent conversation; alias naming is caller-chosen and
	// therefore never used as a signal.
	return strings.TrimSpace(c.SubagentRole) != ""
}

// ResolveLineageWithStore resolves conversationID's parent/root lineage using both
// on-disk .system_generated/subagents/<id>.json descriptors and ConfigStore session
// metadata, automatically re-parenting orphaned /session subagents whose original
// parent conversation was deleted in Antigravity.
func ResolveLineageWithStore(
	resolver LineageResolver,
	store *ConfigStore,
	conversationID string,
	artifactsDirHint string,
	fallbackRootID string,
) ConversationLineage {
	id := strings.TrimSpace(conversationID)
	if id == "" {
		return ConversationLineage{}
	}
	lin := ConversationLineage{
		ConversationID:     id,
		RootConversationID: id,
	}
	brainDir := ""
	if resolver != nil {
		lin = resolver.ResolveLineage(id, artifactsDirHint)
		brainDir = resolver.BrainDir(artifactsDirHint)
	}

	var snap PersistentConfig
	hasSnap := false
	matchedAlias := ""
	var matchedConv ConversationEntry
	if store != nil {
		snap = store.Snapshot()
		hasSnap = true
		for alias, entry := range snap.Conversations {
			if strings.TrimSpace(entry.AGYConversationID) == id {
				matchedAlias = alias
				matchedConv = entry
				if isSessionSubagentEntry(entry) {
					break
				}
			}
		}
	}

	if !lin.IsSubagent && matchedAlias != "" && isSessionSubagentEntry(matchedConv) {
		lin.IsSubagent = true
		lin.SubagentType = firstNonEmptyStr(lin.SubagentType, subagentTypeKubeSession)
		lin.SubagentRole = firstNonEmptyStr(lin.SubagentRole, matchedConv.SubagentRole, humanizeSubagentRole(matchedConv.Alias))
		parentID := strings.TrimSpace(matchedConv.ParentAGYConversationID)
		lin.ParentConversationID = parentID
		lin.RootConversationID = parentID
	} else if lin.IsSubagent && lin.SubagentRole == "" {
		lin.SubagentRole = firstNonEmptyStr(matchedConv.SubagentRole, humanizeSubagentRole(matchedConv.Alias))
	}

	if !lin.IsSubagent {
		return lin
	}

	rootID := strings.TrimSpace(lin.RootConversationID)
	rootAlive := rootID != "" && rootID != id && IsAGYConversationAlive(brainDir, rootID)
	if !rootAlive {
		liveRoot := findLiveRootCoordinatorID(resolver, hasSnap, snap, brainDir, id, fallbackRootID)
		if liveRoot != "" {
			lin.ParentConversationID = liveRoot
			lin.RootConversationID = liveRoot
			if adopter, ok := resolver.(interface {
				AdoptSubagent(brainDir, newParentID, subagentID, role string) error
			}); ok && brainDir != "" {
				_ = adopter.AdoptSubagent(brainDir, liveRoot, id, lin.SubagentRole)
			}
			if store != nil && matchedAlias != "" {
				store.SetConversationParentAGYID(matchedAlias, liveRoot, lin.SubagentRole)
			}
			if brainDir != "" {
				_, _ = ReparentTrajectoryDB(brainDir, id, liveRoot)
			}
		} else if lin.RootConversationID == "" {
			lin.RootConversationID = id
		}
	}
	return lin
}

func findLiveRootCoordinatorID(
	resolver LineageResolver,
	hasSnap bool,
	snap PersistentConfig,
	brainDir string,
	subagentID string,
	fallbackRootID string,
) string {
	isSubagentConv := func(cid string) bool {
		if cid == "" || cid == subagentID {
			return true
		}
		if resolver != nil {
			if l := resolver.ResolveLineage(cid, ""); l.IsSubagent {
				return true
			}
		}
		if hasSnap {
			for _, entry := range snap.Conversations {
				if strings.TrimSpace(entry.AGYConversationID) == cid && isSessionSubagentEntry(entry) {
					return true
				}
			}
		}
		return false
	}

	var candidates []string
	if strings.TrimSpace(fallbackRootID) != "" {
		candidates = append(candidates, strings.TrimSpace(fallbackRootID))
	}
	if hasSnap {
		if active, ok := snap.Conversations[snap.ActiveConversation]; ok && strings.TrimSpace(active.AGYConversationID) != "" {
			candidates = append(candidates, strings.TrimSpace(active.AGYConversationID))
		}
		if strings.TrimSpace(snap.Push.AGYConversationID) != "" {
			candidates = append(candidates, strings.TrimSpace(snap.Push.AGYConversationID))
		}
	}
	for _, cand := range candidates {
		if cand == "" || cand == subagentID {
			continue
		}
		if !IsAGYConversationAlive(brainDir, cand) {
			continue
		}
		if isSubagentConv(cand) {
			continue
		}
		return cand
	}
	if hasSnap {
		if fallbackID, _ := mostRecentLiveCoordinatorEntry(resolver, snap, brainDir, subagentID); fallbackID != "" {
			return fallbackID
		}
	}
	return ""
}

// mostRecentLiveCoordinatorEntry scans snap.Conversations for the most recently active
// live non-subagent root coordinator conversation when the previously active coordinator
// conversation was deleted in Antigravity.
func mostRecentLiveCoordinatorEntry(
	resolver LineageResolver,
	snap PersistentConfig,
	brainDir string,
	excludeID string,
) (string, string) {
	bDir := strings.TrimSpace(brainDir)
	if bDir == "" || len(snap.Conversations) == 0 {
		return "", ""
	}
	var bestID, bestAlias string
	var bestTime time.Time
	for alias, entry := range snap.Conversations {
		cid := strings.TrimSpace(entry.AGYConversationID)
		if cid == "" || cid == excludeID || entry.SessionTerminated || isSessionSubagentEntry(entry) {
			continue
		}
		if resolver != nil {
			if l := resolver.ResolveLineage(cid, ""); l.IsSubagent {
				continue
			}
		}
		if !IsAGYConversationAlive(bDir, cid) {
			continue
		}
		if bestID == "" || entry.LastUsedAt.After(bestTime) || (entry.LastUsedAt.Equal(bestTime) && alias < bestAlias) {
			bestID = cid
			bestAlias = alias
			bestTime = entry.LastUsedAt
		}
	}
	return bestID, bestAlias
}

// ReparentTrajectoryDB updates CortexTrajectoryMetadata.parent_conversation_id (field 5)
// and CortexTrajectoryMetadata.root_conversation_id (field 6) in-place inside
// <brainDir>/../conversations/<subagentID>.db (and .db-wal if present).
// Because canonical UUIDs are always 36 ASCII bytes, the 76-byte protobuf wire block
// (\x2a\x24<UUID>\x32\x24<UUID>) is overwritten at its exact file offset via WriteAt,
// preserving the SQLite file inode, B-tree page layout, and WAL locks.
func ReparentTrajectoryDB(brainDir, subagentID, newParentID string) (bool, error) {
	bDir := strings.TrimSpace(brainDir)
	subID := strings.TrimSpace(subagentID)
	parentID := strings.TrimSpace(newParentID)
	if bDir == "" || !isCanonicalUUID(subID) || !isCanonicalUUID(parentID) || subID == parentID {
		return false, nil
	}
	convDir := filepath.Join(filepath.Dir(filepath.Clean(bDir)), "conversations")
	changedAny := false
	var firstErr error
	for _, suffix := range []string{".db", ".db-wal"} {
		dbPath := filepath.Join(convDir, subID+suffix)
		changed, err := reparentSQLiteFileInPlace(dbPath, parentID)
		if err != nil && !os.IsNotExist(err) && firstErr == nil {
			firstErr = err
		}
		if changed {
			changedAny = true
		}
	}
	return changedAny, firstErr
}

func reparentSQLiteFileInPlace(path string, newParentID string) (bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return false, err
	}
	offsets := findTrajectoryMetadataPatchOffsets(data, newParentID)
	if len(offsets) == 0 {
		return false, nil
	}
	replacement := buildTrajectoryMetadataPatchBlock(newParentID)
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return false, err
	}
	defer f.Close()
	for _, off := range offsets {
		if _, err := f.WriteAt(replacement, int64(off)); err != nil {
			return false, err
		}
	}
	return true, nil
}

func buildTrajectoryMetadataPatchBlock(parentID string) []byte {
	buf := make([]byte, 0, metadataPatchBlockLen)
	buf = append(buf, 0x2a, 0x24)
	buf = append(buf, []byte(parentID)...)
	buf = append(buf, 0x32, 0x24)
	buf = append(buf, []byte(parentID)...)
	return buf
}

func findTrajectoryMetadataPatchOffsets(data []byte, newParentID string) []int {
	if len(data) < metadataPatchBlockLen || !isCanonicalUUID(newParentID) {
		return nil
	}
	targetBytes := []byte(newParentID)
	var offsets []int
	limit := len(data) - metadataPatchBlockLen
	for i := 0; i <= limit; i++ {
		if data[i] != 0x2a || data[i+1] != 0x24 || data[i+38] != 0x32 || data[i+39] != 0x24 {
			continue
		}
		parentSlice := data[i+2 : i+38]
		rootSlice := data[i+40 : i+76]
		if !isCanonicalUUIDBytes(parentSlice) || !isCanonicalUUIDBytes(rootSlice) {
			continue
		}
		if !bytes.Equal(parentSlice, targetBytes) || !bytes.Equal(rootSlice, targetBytes) {
			offsets = append(offsets, i)
		}
		i += metadataPatchBlockLen - 1
	}
	return offsets
}

// ReparentLiveAGYSummaries updates the running Antigravity Language Server's in-memory
// Language Server's in-memory trajectory summaries so the Antigravity Web UI top-left parent
// breadcrumb immediately navigates to newParentID ("Connect to my-cluster" etc.)
// instead of a deleted or stale parent conversation.
// It returns a map of subagentID -> current CascadeRunStatus string observed on the server.
func ReparentLiveAGYSummaries(
	ctx context.Context,
	lsBaseURL string,
	newParentID string,
	subagentRoles map[string]string,
	forceWrite bool,
) (map[string]string, error) {
	parentID := strings.TrimSpace(newParentID)
	if !isCanonicalUUID(parentID) || len(subagentRoles) == 0 {
		return nil, nil
	}
	baseURL := strings.TrimRight(firstNonEmptyStr(lsBaseURL, os.Getenv(envAGYLanguageServerURL), defaultAGYLSBaseURL), "/")
	if baseURL == "" {
		return nil, nil
	}

	client := &http.Client{Timeout: lsHTTPTimeout}
	csrf, err := fetchAGYCSRFToken(ctx, client, baseURL)
	if err != nil || csrf == "" {
		return nil, err
	}

	summaries, err := fetchAllCascadeTrajectorySummaries(ctx, client, baseURL, csrf)
	if err != nil {
		return nil, err
	}

	// Pre-warm any cold/unloaded subagent trajectories in CascadeManager via LoadTrajectory
	// so GetAllCascadeTrajectories includes them (loaded from the already-patched .db on disk)
	// and clicking the subagent in the Swarm UI opens its history immediately on the first click.
	loadedAnyCold := false
	for rawSubID := range subagentRoles {
		subID := strings.TrimSpace(rawSubID)
		if !isCanonicalUUID(subID) || subID == parentID {
			continue
		}
		if _, ok := summaries[subID]; !ok {
			if loadAGYTrajectory(ctx, client, baseURL, csrf, subID) == nil {
				loadedAnyCold = true
			}
		}
	}
	if loadedAnyCold {
		if refreshed, refErr := fetchAllCascadeTrajectorySummaries(ctx, client, baseURL, csrf); refErr == nil {
			summaries = refreshed
		}
	}

	statuses := make(map[string]string, len(subagentRoles))
	var firstWriteErr error
	for rawSubID, role := range subagentRoles {
		subID := strings.TrimSpace(rawSubID)
		if !isCanonicalUUID(subID) || subID == parentID {
			continue
		}
		summary, ok := summaries[subID]
		if !ok || summary == nil {
			continue
		}
		if st, _ := summary["status"].(string); st != "" {
			statuses[subID] = st
		}
		meta, _ := summary["trajectoryMetadata"].(map[string]any)
		if meta == nil {
			meta = make(map[string]any)
			summary["trajectoryMetadata"] = meta
		}
		curParent, _ := meta["parentConversationId"].(string)
		curRoot, _ := meta["rootConversationId"].(string)
		curTitle, _ := summary["summary"].(string)
		needTitle := strings.TrimSpace(curTitle) == "" && strings.TrimSpace(role) != ""

		if !forceWrite && curParent == parentID && curRoot == parentID && !needTitle {
			continue
		}
		meta["parentConversationId"] = parentID
		meta["rootConversationId"] = parentID
		if needTitle {
			summary["summary"] = strings.TrimSpace(role)
		}
		if err := writeTrajectorySummary(ctx, client, baseURL, csrf, subID, summary); err != nil && firstWriteErr == nil {
			firstWriteErr = err
		}
	}
	return statuses, firstWriteErr
}

func fetchAGYCSRFToken(ctx context.Context, client *http.Client, baseURL string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/", nil)
	if err == nil {
		if resp, doErr := client.Do(req); doErr == nil {
			body, readErr := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
			_ = resp.Body.Close()
			if readErr == nil {
				if m := csrfTokenHTMLRe.FindSubmatch(body); len(m) >= 2 {
					return strings.TrimSpace(string(m[1])), nil
				}
			}
		}
	}
	if envToken := strings.TrimSpace(os.Getenv(envAntigravityCSRFToken)); envToken != "" {
		return envToken, nil
	}
	return "", fmt.Errorf("csrfToken not found at %s", baseURL)
}

func fetchAllCascadeTrajectorySummaries(
	ctx context.Context,
	client *http.Client,
	baseURL string,
	csrf string,
) (map[string]map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+rpcGetAllTrajectoriesPath, bytes.NewReader([]byte("{}")))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Connect-Protocol-Version", "1")
	req.Header.Set("x-codeium-csrf-token", csrf)

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GetAllCascadeTrajectories status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 16*1024*1024))
	if err != nil {
		return nil, err
	}
	var parsed struct {
		TrajectorySummaries map[string]map[string]any `json:"trajectorySummaries"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, err
	}
	if parsed.TrajectorySummaries == nil {
		parsed.TrajectorySummaries = make(map[string]map[string]any)
	}
	return parsed.TrajectorySummaries, nil
}

func loadAGYTrajectory(
	ctx context.Context,
	client *http.Client,
	baseURL string,
	csrf string,
	cascadeID string,
) error {
	payload, err := json.Marshal(map[string]any{
		"cascadeId": cascadeID,
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+rpcLoadTrajectoryPath, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Connect-Protocol-Version", "1")
	req.Header.Set("x-codeium-csrf-token", csrf)

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("LoadTrajectory status %d", resp.StatusCode)
	}
	return nil
}

func writeTrajectorySummary(
	ctx context.Context,
	client *http.Client,
	baseURL string,
	csrf string,
	cascadeID string,
	summary map[string]any,
) error {
	payload, err := json.Marshal(map[string]any{
		"cascadeId": cascadeID,
		"summary":   summary,
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+rpcWriteSummaryPath, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Connect-Protocol-Version", "1")
	req.Header.Set("x-codeium-csrf-token", csrf)

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("write trajectory summary: status %d", resp.StatusCode)
	}
	return nil
}

// DeleteAGYCascadeTrajectory deletes a subagent conversation trajectory in the running
// Antigravity Language Server via DeleteCascadeTrajectory so it is removed from the Subagents list
// and conversation store when the user deletes a session.
func DeleteAGYCascadeTrajectory(ctx context.Context, lsBaseURL string, cascadeID string) error {
	id := strings.TrimSpace(cascadeID)
	if !isCanonicalUUID(id) {
		return nil
	}
	baseURL := strings.TrimRight(firstNonEmptyStr(lsBaseURL, os.Getenv(envAGYLanguageServerURL), defaultAGYLSBaseURL), "/")
	if baseURL == "" {
		return nil
	}
	client := &http.Client{Timeout: lsHTTPTimeout}
	csrf, err := fetchAGYCSRFToken(ctx, client, baseURL)
	if err != nil || csrf == "" {
		return err
	}
	payload, err := json.Marshal(map[string]any{
		"cascadeId": id,
	})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+rpcDeleteTrajectoryPath, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Connect-Protocol-Version", "1")
	req.Header.Set("x-codeium-csrf-token", csrf)

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("DeleteCascadeTrajectory status %d", resp.StatusCode)
	}
	return nil
}

// IsAGYCascadeUserCanceled checks whether an active Antigravity conversation (cascadeID)
// has been stopped/canceled by the user ("User cancelled agent execution.") while a
// remote kube-agents task was still streaming.
// It inspects both the on-disk transcript.jsonl tail and the Language Server's live
// GetAllCascadeTrajectories status.
func IsAGYCascadeUserCanceled(ctx context.Context, lsBaseURL, brainDir, cascadeID string) bool {
	id := strings.TrimSpace(cascadeID)
	if id == "" {
		return false
	}
	explicitCanceled, incompleteToolTurn := inspectTranscriptTailForCancel(brainDir, id)
	if explicitCanceled {
		return true
	}
	if !isCanonicalUUID(id) {
		return false
	}
	baseURL := strings.TrimRight(firstNonEmptyStr(lsBaseURL, os.Getenv(envAGYLanguageServerURL), defaultAGYLSBaseURL), "/")
	if baseURL == "" {
		return false
	}
	client := &http.Client{Timeout: lsHTTPTimeout}
	csrf, err := fetchAGYCSRFToken(ctx, client, baseURL)
	if err != nil || csrf == "" {
		return false
	}
	summaries, err := fetchAllCascadeTrajectorySummaries(ctx, client, baseURL, csrf)
	if err != nil {
		return false
	}
	summary, ok := summaries[id]
	if !ok || summary == nil {
		return false
	}
	st, _ := summary["status"].(string)
	if st == "" || st == "CASCADE_RUN_STATUS_RUNNING" {
		return false
	}
	// If the cascade is no longer RUNNING in Language Server (e.g. CASCADE_RUN_STATUS_IDLE
	// or CASCADE_RUN_STATUS_CANCELED) while the transcript tail still ends on a tool-calling
	// step (or has no final assistant text turn), the user stopped/canceled execution.
	return incompleteToolTurn || strings.Contains(strings.ToUpper(st), "CANCEL")
}

func inspectTranscriptTailForCancel(brainDir, cascadeID string) (explicitCanceled bool, incompleteToolTurn bool) {
	bDir := strings.TrimSpace(brainDir)
	id := strings.TrimSpace(cascadeID)
	if bDir == "" || id == "" {
		return false, true
	}
	transcriptPath := filepath.Join(bDir, id, systemGeneratedDirName, "logs", "transcript.jsonl")
	f, err := os.Open(transcriptPath)
	if err != nil {
		return false, true
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil || info.Size() == 0 {
		return false, true
	}
	const tailWindow = 32 * 1024
	offset := int64(0)
	if info.Size() > tailWindow {
		offset = info.Size() - tailWindow
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return false, true
	}
	raw, err := io.ReadAll(io.LimitReader(f, tailWindow+1024))
	if err != nil || len(raw) == 0 {
		return false, true
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line == "" || !strings.HasPrefix(line, "{") {
			continue
		}
		var step struct {
			Type      string          `json:"type"`
			Status    string          `json:"status"`
			Content   string          `json:"content"`
			ToolCalls json.RawMessage `json:"tool_calls"`
		}
		if err := json.Unmarshal([]byte(line), &step); err != nil {
			continue
		}
		upperStatus := strings.ToUpper(strings.TrimSpace(step.Status))
		lowerContent := strings.ToLower(step.Content)
		if strings.Contains(upperStatus, "CANCEL") ||
			strings.Contains(lowerContent, "user cancelled agent execution") ||
			strings.Contains(lowerContent, "user canceled agent execution") ||
			strings.Contains(lowerContent, "cancelled by user") {
			return true, true
		}
		if step.Type == "PLANNER_RESPONSE" {
			tc := strings.TrimSpace(string(step.ToolCalls))
			hasToolCalls := tc != "" && tc != "null" && tc != "[]"
			return false, hasToolCalls
		}
		if step.Type == "USER_INPUT" {
			return false, true
		}
	}
	return false, true
}

// EnsureAGYMCPServerReady queries the Antigravity Language Server's GetMcpServerStates RPC
// and, if the kube-agents MCP server is in MCP_SERVER_STATUS_ERROR (or missing), triggers
// RefreshMcpServers so the Language Server immediately respawns the kube-agents-mcp process
// before the turn's tool callers are bound.
func EnsureAGYMCPServerReady(ctx context.Context, lsBaseURL string) error {
	baseURL := strings.TrimRight(firstNonEmptyStr(lsBaseURL, os.Getenv(envAGYLanguageServerURL), defaultAGYLSBaseURL), "/")
	if baseURL == "" {
		return nil
	}
	client := &http.Client{Timeout: lsHTTPTimeout}
	csrf, err := fetchAGYCSRFToken(ctx, client, baseURL)
	if err != nil || csrf == "" {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+rpcGetMcpServerStatesPath, bytes.NewReader([]byte("{}")))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Connect-Protocol-Version", "1")
	req.Header.Set("x-codeium-csrf-token", csrf)

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 4*1024*1024))
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || readErr != nil {
		return fmt.Errorf("GetMcpServerStates status %d", resp.StatusCode)
	}

	var parsed struct {
		States []struct {
			Spec struct {
				ServerName string `json:"serverName"`
			} `json:"spec"`
			Status string `json:"status"`
		} `json:"states"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return err
	}
	isDefaultProdLS := strings.TrimSpace(lsBaseURL) == "" && strings.TrimSpace(os.Getenv(envAGYLanguageServerURL)) == ""
	hasLive := !isDefaultProdLS || hasLiveMCPServerProcess()
	targetServerName := mcpServerName
	for _, st := range parsed.States {
		name := strings.TrimSpace(st.Spec.ServerName)
		if name == mcpServerName || name == mcpServerName+"_"+mcpServerName || strings.HasSuffix(name, "_"+mcpServerName) {
			targetServerName = name
			if st.Status == "MCP_SERVER_STATUS_READY" && hasLive {
				return nil
			}
		}
	}

	refreshPayload, _ := json.Marshal(map[string]any{
		"serverName": targetServerName,
	})
	refReq, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+rpcRefreshMcpServersPath, bytes.NewReader(refreshPayload))
	if err != nil {
		return err
	}
	refReq.Header.Set("Content-Type", "application/json")
	refReq.Header.Set("Connect-Protocol-Version", "1")
	refReq.Header.Set("x-codeium-csrf-token", csrf)

	refResp, err := client.Do(refReq)
	if err != nil {
		return err
	}
	defer refResp.Body.Close()
	_, _ = io.Copy(io.Discard, refResp.Body)
	if refResp.StatusCode != http.StatusOK {
		return fmt.Errorf("RefreshMcpServers status %d", refResp.StatusCode)
	}
	return nil
}

// hasLiveMCPServerProcess checks /proc to see if any long-running kube-agents MCP stdio
// server process (excluding short-lived --hook invocations and the current PID) is alive on
// the host. A process counts when it runs the same executable as this hook (the hook and the
// server are one binary), compared by resolved path rather than by file name so a renamed or
// relocated install still matches. When a running server process was launched via a release
// symlink (such as a package-manager "latest" link) that now resolves to selfExe while its
// /proc/<pid>/exe still points to an older image, or when its binary was replaced in-place
// (" (deleted)"), it sends SIGTERM to the stale process and returns false so RefreshMcpServers
// hot-restarts the upgraded binary. If /proc is unavailable (any non-Linux host), it returns true.
func hasLiveMCPServerProcess() bool {
	selfExe := resolvedExecutablePath()
	if selfExe == "" {
		return true
	}
	selfPID := fmt.Sprintf("%d", os.Getpid())
	return scanLiveMCPServerProcess("/proc", selfExe, selfPID, terminateStaleMCPServerProcess)
}

func scanLiveMCPServerProcess(procRoot, selfExe, selfPID string, killStale func(int)) bool {
	entries, err := os.ReadDir(procRoot)
	if err != nil {
		return true
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		pid := entry.Name()
		if pid == selfPID || pid == "" || pid[0] < '0' || pid[0] > '9' {
			continue
		}
		exe, err := os.Readlink(filepath.Join(procRoot, pid, "exe"))
		if err != nil {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(procRoot, pid, "cmdline"))
		if err != nil || len(raw) == 0 {
			continue
		}
		parts := strings.Split(strings.TrimRight(string(raw), "\x00"), "\x00")
		isHook := false
		for _, arg := range parts[1:] {
			if arg == "--hook" || arg == "-hook" {
				isHook = true
				break
			}
		}

		isDeletedSelf := strings.HasSuffix(exe, " (deleted)") && strings.TrimSuffix(exe, " (deleted)") == selfExe
		isUpgradedSymlinkSelf := false
		if !isDeletedSelf && exe != selfExe && len(parts) > 0 && filepath.IsAbs(parts[0]) {
			if resolvedCmd, rErr := filepath.EvalSymlinks(parts[0]); rErr == nil && resolvedCmd == selfExe {
				isUpgradedSymlinkSelf = true
			}
		}
		if !isDeletedSelf && !isUpgradedSymlinkSelf && exe != selfExe {
			continue
		}
		if isDeletedSelf || isUpgradedSymlinkSelf {
			if !isHook && killStale != nil {
				if pidNum, pErr := strconv.Atoi(pid); pErr == nil && pidNum > 1 {
					killStale(pidNum)
				}
			}
			continue
		}
		if !isHook {
			return true
		}
	}
	return false
}

func terminateStaleMCPServerProcess(pidNum int) {
	if err := syscall.Kill(pidNum, syscall.SIGTERM); err != nil {
		return
	}
	procPath := filepath.Join("/proc", strconv.Itoa(pidNum))
	deadline := time.Now().Add(500 * time.Millisecond)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(procPath); os.IsNotExist(err) {
			time.Sleep(25 * time.Millisecond)
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// resolvedExecutablePath returns this process's executable with symlinks resolved, or "" when
// it cannot be determined.
func resolvedExecutablePath() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		return resolved
	}
	return exe
}
