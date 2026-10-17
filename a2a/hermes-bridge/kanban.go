package hermesbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	kanbanPollInterval        = time.Second
	kanbanOutOfTurnInterval   = 2 * time.Second
	kanbanMaxEmptyPolls       = 3
	kanbanOutOfTurnWindowSecs = 7200
	hermesVenvPythonPath      = "/opt/hermes/.venv/bin/python3"
	maxTrackedKanbanEntries   = 2048

	cardAnswerSubstantiveChars = 600
	cardSynthesisInputCap      = 120 << 10
	cardSynthesisTimeout       = 4 * time.Minute
	cardTurnArtifactStride     = 1000
	cardResultsHeader          = "Specialist card results (raw):\n\n"
	silentSentinel             = "[SILENT]"
)

type kanbanTracker struct {
	mu               sync.Mutex
	activeWaits      map[string]int
	deliveredIDs     map[string]bool
	pendingSettle    map[string]int
	canceledSessions map[string]bool
	sessionEpochs    map[string]int
	consoleSessions  map[string]consoleSessionMeta
}

type kanbanCardRow struct {
	ID              string `json:"id"`
	Title           string `json:"title"`
	Status          string `json:"status"`
	Assignee        string `json:"assignee"`
	SessionID       string `json:"session_id"`
	WorkerSessionID string `json:"worker_session_id"`
	Verdict         string `json:"verdict"`
	Result          string `json:"result"`
	Err             string `json:"err"`
	BlockPayload    string `json:"block_payload"`
}

type consoleSessionMeta struct {
	ContextID     string
	CorrelationID string
	Authority     json.RawMessage
}

const kanbanDBPyScript = `
import json, os, re, sqlite3, subprocess, sys, time

mode, db_path, session_id = sys.argv[1], sys.argv[2], sys.argv[3]
settled = ("done", "archived", "completed", "blocked", "failed", "gave_up", "canceled", "cancelled")
settled_sql = ",".join(repr(s) for s in settled)

if mode == "cancel":
    if not os.path.exists(db_path):
        sys.exit(0)
    card_ids = []
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(tasks)")}
        sess_col = "creator_session_id" if "creator_session_id" in cols else ("session_id" if "session_id" in cols else "origin_session_id")
        rows = conn.execute(f"SELECT id FROM tasks WHERE {sess_col} = ? AND LOWER(COALESCE(status, '')) NOT IN ({settled_sql})", (session_id,)).fetchall()
        card_ids = [r[0] for r in rows if r and r[0]]
        if card_ids and "parent_id" in cols:
            ph = ",".join("?" for _ in card_ids)
            for cr in conn.execute(f"SELECT id FROM tasks WHERE parent_id IN ({ph}) AND LOWER(COALESCE(status, '')) NOT IN ({settled_sql})", card_ids).fetchall():
                if cr and cr[0] and cr[0] not in card_ids:
                    card_ids.append(cr[0])
        if card_ids:
            ph = ",".join("?" for _ in card_ids)
            upd, params = ["status = 'canceled'"], []
            if "updated_at" in cols:
                upd.append("updated_at = ?")
                params.append(int(time.time()))
            err_col = next((c for c in ("last_failure_error", "error", "err") if c in cols), None)
            if err_col:
                upd.append(f"{err_col} = 'Canceled by user (A2A task stopped)'")
            conn.execute(f"UPDATE tasks SET {', '.join(upd)} WHERE id IN ({ph})", params + card_ids)
            conn.commit()
        conn.close()
    except Exception:
        pass
    for cid in card_ids:
        if re.fullmatch(r"t_[A-Za-z0-9_-]{1,64}", str(cid)):
            try:
                subprocess.run(["pkill", "-TERM", "-f", f"/kanban/workspaces/{cid}"], check=False, timeout=2)
            except Exception:
                pass
    sys.exit(0)

since_ts = float(sys.argv[4])
tracked = [x for x in (sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else []) if x]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
conn.row_factory = sqlite3.Row
cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
sess_col = "creator_session_id" if "creator_session_id" in cols else "session_id"
err_col = "last_failure_error" if "last_failure_error" in cols else "''"
assignee_col = "assignee" if "assignee" in cols else "''"
select_sql = f"SELECT id, title, status, COALESCE({assignee_col}, '') AS assignee, COALESCE({sess_col}, '') AS session_id, COALESCE(result, '') AS result, COALESCE({err_col}, '') AS err FROM tasks"
if session_id == "*":
    rows = conn.execute(f"{select_sql} WHERE {sess_col} LIKE 'a2a-%' AND (created_at >= ? OR LOWER(COALESCE(status, '')) NOT IN ({settled_sql})) ORDER BY created_at ASC, id ASC", (since_ts,)).fetchall()
else:
    conds, params = ["created_at >= ?"], [session_id, since_ts]
    if tracked:
        conds.append(f"id IN ({','.join('?' for _ in tracked)})")
        params.extend(tracked)
    rows = conn.execute(f"{select_sql} WHERE {sess_col} = ? AND ({' OR '.join(conds)}) ORDER BY created_at ASC, id ASC", params).fetchall()

run_cols = set()
try:
    run_cols = {c[1] for c in conn.execute("PRAGMA table_info(task_runs)")}
except Exception:
    pass
ws_col = "session_id" if "session_id" in run_cols else ("worker_session_id" if "worker_session_id" in run_cols else "''")
verd_col = "verdict" if "verdict" in run_cols else "''"
meta_col = "metadata" if "metadata" in run_cols else "''"

out = []
for r in rows:
    item = dict(r)
    if run_cols:
        try:
            rr = conn.execute(
                f"SELECT COALESCE(summary, '') AS summary, COALESCE(error, '') AS error, COALESCE({ws_col}, '') AS worker_session_id, COALESCE({verd_col}, '') AS verdict, COALESCE({meta_col}, '') AS metadata "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (r["id"],)
            ).fetchone()
            if rr:
                item["result"] = item["result"] or rr["summary"]
                item["err"] = item["err"] or rr["error"]
                if rr["worker_session_id"]:
                    item["worker_session_id"] = rr["worker_session_id"]
                if rr["verdict"]:
                    item["verdict"] = rr["verdict"]
                if rr["metadata"]:
                    meta = json.loads(rr["metadata"])
                    if isinstance(meta, dict):
                        if not item.get("worker_session_id") and meta.get("worker_session_id"):
                            item["worker_session_id"] = str(meta["worker_session_id"])
                        if not item.get("verdict"):
                            for vk in ("health_status", "verdict", "status"):
                                if meta.get(vk):
                                    item["verdict"] = str(meta[vk])
                                    break
        except Exception:
            pass
    try:
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'gave_up') ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        if ev and ev[0]:
            item["block_payload"] = ev[0]
    except Exception:
        pass
    out.append(item)
print(json.dumps(out))
`

func (k *kanbanTracker) ensureMapsLocked() {
	if k.activeWaits == nil {
		k.activeWaits = make(map[string]int)
		k.deliveredIDs = make(map[string]bool)
		k.pendingSettle = make(map[string]int)
		k.canceledSessions = make(map[string]bool)
		k.sessionEpochs = make(map[string]int)
		k.consoleSessions = make(map[string]consoleSessionMeta)
	}
	if len(k.deliveredIDs) > maxTrackedKanbanEntries {
		clear(k.deliveredIDs)
	}
	if len(k.canceledSessions) > maxTrackedKanbanEntries {
		clear(k.canceledSessions)
	}
	if len(k.sessionEpochs) > maxTrackedKanbanEntries {
		clear(k.sessionEpochs)
	}
	if len(k.consoleSessions) > maxTrackedKanbanEntries {
		clear(k.consoleSessions)
	}
}

func (b *Bridge) sessionIDForContext(contextID string) string {
	base := apiSessionID(contextID)
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	if epoch := b.kanban.sessionEpochs[contextID]; epoch > 0 {
		return fmt.Sprintf("%s-e%d", base, epoch)
	}
	return base
}

func (b *Bridge) markSessionCanceled(contextID, sessionID string) {
	b.kanban.mu.Lock()
	b.kanban.ensureMapsLocked()
	b.kanban.canceledSessions[sessionID] = true
	if contextID != "" {
		b.kanban.sessionEpochs[contextID]++
	}
	b.kanban.mu.Unlock()

	go b.abortCanceledSessionKanbanCards(sessionID)
}

func (b *Bridge) abortCanceledSessionKanbanCards(sessionID string) {
	dbPath := resolveKanbanDBPath()
	if dbPath == "" || sessionID == "" {
		return
	}
	pythonBin := resolvePythonBin()
	for i := 0; i < 4; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = exec.CommandContext(ctx, pythonBin, "-c", kanbanDBPyScript, "cancel", dbPath, sessionID).Run()
		cancel()
		if i < 3 {
			time.Sleep(1500 * time.Millisecond)
		}
	}
}

func (b *Bridge) rememberConsoleSession(sessionID string, origin *lib.Envelope) {
	if sessionID == "" || origin == nil {
		return
	}
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	b.kanban.consoleSessions[sessionID] = consoleSessionMeta{
		ContextID:     origin.ContextID,
		CorrelationID: origin.CorrelationID,
		Authority:     append(json.RawMessage(nil), origin.Authority...),
	}
}

func (b *Bridge) beginKanbanWait(sessionID string) {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	b.kanban.activeWaits[sessionID]++
}

func (b *Bridge) endKanbanWait(sessionID string) {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	if b.kanban.activeWaits[sessionID] > 0 {
		b.kanban.activeWaits[sessionID]--
		if b.kanban.activeWaits[sessionID] == 0 {
			delete(b.kanban.activeWaits, sessionID)
		}
	}
}

func (b *Bridge) markKanbanCardDelivered(cardID string) {
	if cardID == "" {
		return
	}
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	b.kanban.deliveredIDs[cardID] = true
	delete(b.kanban.pendingSettle, cardID)
}

func (b *Bridge) isKanbanCardDelivered(cardID string) bool {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	return b.kanban.deliveredIDs[cardID]
}

func (b *Bridge) shouldDeliverOutOfTurnKanbanCard(cardID, sessionID string) (consoleSessionMeta, bool) {
	b.kanban.mu.Lock()
	defer b.kanban.mu.Unlock()
	b.kanban.ensureMapsLocked()
	if b.kanban.deliveredIDs[cardID] || b.kanban.canceledSessions[sessionID] {
		return consoleSessionMeta{}, false
	}
	if b.kanban.activeWaits[sessionID] > 0 {
		delete(b.kanban.pendingSettle, cardID)
		return consoleSessionMeta{}, false
	}
	b.kanban.pendingSettle[cardID]++
	if b.kanban.pendingSettle[cardID] < 2 {
		return consoleSessionMeta{}, false
	}
	b.kanban.deliveredIDs[cardID] = true
	delete(b.kanban.pendingSettle, cardID)
	meta := b.kanban.consoleSessions[sessionID]
	if meta.ContextID == "" && strings.HasPrefix(sessionID, apiSessionIDPrefix) {
		meta.ContextID = strings.TrimPrefix(sessionID, apiSessionIDPrefix)
	}
	return meta, true
}

func hasQueuedSteer(run *taskRun) bool {
	run.mu.Lock()
	defer run.mu.Unlock()
	return run.state == stateRunning && len(run.steers) > 0
}

func resolveKanbanDBPath() string {
	for _, envKey := range []string{"HERMES_KANBAN_DB", "HERMES_HOME", "PLATFORM_AGENT_HOME"} {
		if v := strings.TrimSpace(os.Getenv(envKey)); v != "" {
			p := v
			if envKey != "HERMES_KANBAN_DB" {
				p = filepath.Join(v, "kanban.db")
			}
			if info, err := os.Stat(p); err == nil && !info.IsDir() {
				return p
			}
		}
	}
	if info, err := os.Stat("/opt/data/kanban.db"); err == nil && !info.IsDir() {
		return "/opt/data/kanban.db"
	}
	return ""
}

func resolvePythonBin() string {
	if info, err := os.Stat(hermesVenvPythonPath); err == nil && !info.IsDir() {
		return hermesVenvPythonPath
	}
	return "python3"
}

func isKanbanStatusSettled(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "done", "archived", "completed", "blocked", "failed", "gave_up", "canceled", "cancelled":
		return true
	default:
		return false
	}
}

func inferKanbanCardVerdict(c kanbanCardRow) string {
	if v := strings.TrimSpace(c.Verdict); v != "" {
		return strings.ToLower(v)
	}
	if st := strings.ToLower(strings.TrimSpace(c.Status)); st != "" {
		return st
	}
	return "completed"
}

func formatKanbanCardOutput(c kanbanCardRow) string {
	if res := strings.TrimSpace(c.Result); res != "" {
		return res
	}
	if bp := strings.TrimSpace(c.BlockPayload); bp != "" {
		var parsed map[string]any
		if json.Unmarshal([]byte(bp), &parsed) == nil {
			for _, k := range []string{"question", "reason", "message", "summary"} {
				if v, ok := parsed[k].(string); ok && strings.TrimSpace(v) != "" {
					return strings.TrimSpace(v)
				}
			}
		}
		return bp
	}
	return strings.TrimSpace(c.Err)
}

func querySessionKanbanCards(ctx context.Context, pythonBin, dbPath, sessionID string, sinceUnix int64, trackedIDs []string) ([]kanbanCardRow, error) {
	qCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(qCtx, pythonBin, "-c", kanbanDBPyScript, "poll", dbPath, sessionID, strconv.FormatInt(sinceUnix, 10), strings.Join(trackedIDs, ","))
	out, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	var rows []kanbanCardRow
	if err := json.Unmarshal(out, &rows); err != nil {
		return nil, err
	}
	return rows, nil
}

func (b *Bridge) waitForKanbanCards(run *taskRun, taskCtx context.Context, act *activityState, sessionID string, turnStartUnix int64, turn int, ackText string) (string, bool) {
	dbPath := resolveKanbanDBPath()
	if dbPath == "" {
		return ackText, true
	}
	pythonBin := resolvePythonBin()

	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return "", false
	}
	run.answerHeld = false
	run.mu.Unlock()

	act.inTurn.Store(false)
	act.waitingCard.Store(true)
	defer func() {
		act.waitingCard.Store(false)
		act.inTurn.Store(true)
	}()

	var trackedIDs []string
	trackedSet := make(map[string]bool)
	reportedSettled := make(map[string]bool)
	sawUnsettled := false
	emptyPolls := 0
	ticker := time.NewTicker(kanbanPollInterval)
	defer ticker.Stop()

	for {
		cards, err := querySessionKanbanCards(taskCtx, pythonBin, dbPath, sessionID, turnStartUnix, trackedIDs)
		if err == nil {
			var relevant []kanbanCardRow
			for _, c := range cards {
				if isKanbanStatusSettled(c.Status) && !trackedSet[c.ID] && b.isKanbanCardDelivered(c.ID) {
					continue
				}
				if !trackedSet[c.ID] {
					trackedSet[c.ID] = true
					trackedIDs = append(trackedIDs, c.ID)
				}
				relevant = append(relevant, c)
			}
			if len(relevant) == 0 {
				if !act.cardsFiled.Load() {
					break
				}
				emptyPolls++
				if emptyPolls >= kanbanMaxEmptyPolls {
					break
				}
			} else {
				settledCount := 0
				for _, c := range relevant {
					act.SyncKanbanCardDetails(c.ID, c.Title, c.Assignee, c.WorkerSessionID)
					if isKanbanStatusSettled(c.Status) {
						settledCount++
					}
				}
				totalCount := len(relevant)
				if settledCount < totalCount {
					sawUnsettled = true
				}
				for _, c := range relevant {
					if !isKanbanStatusSettled(c.Status) || reportedSettled[c.ID] {
						continue
					}
					reportedSettled[c.ID] = true
					shortTitle := shortCardClusterOrTitle(c.Title)
					if shortTitle == "" {
						shortTitle = c.ID
					}
					verdict := inferKanbanCardVerdict(c)
					summary := oneLinePreview(scrubInlineSecrets(formatKanbanCardOutput(c)), 220)
					stepTitle := fmt.Sprintf("Specialist check completed: %s (%s — %d/%d cards done).", shortTitle, verdict, settledCount, totalCount)
					thinkingText := stepTitle
					if summary != "" {
						thinkingText = stepTitle + "\t" + summary
					}
					progText := fmt.Sprintf("Kanban specialist checks: %d/%d completed (%d running) — latest: %s (%s)",
						settledCount, totalCount, totalCount-settledCount, shortTitle, verdict)
					run.mu.Lock()
					if run.state == stateRunning {
						if act.thinkingUnderBudget() {
							b.publishArtifactPart(run, lib.ArtifactThinking, lib.Part{Kind: "text", Text: thinkingText})
						}
						if act.heartbeatUnderBudget() {
							b.publishProgress(run, progText)
						}
					}
					run.mu.Unlock()
				}

				if settledCount == totalCount {
					var parts []string
					for _, c := range relevant {
						b.markKanbanCardDelivered(c.ID)
						if out := formatKanbanCardOutput(c); out != "" {
							parts = append(parts, out)
						}
					}
					if len(parts) > 0 {
						synthStep := fmt.Sprintf("Synthesizing findings across %d specialist card(s).\tCombining specialist reports into unified fleet response.", len(relevant))
						run.mu.Lock()
						if run.state == stateRunning && act.thinkingUnderBudget() {
							b.publishArtifactPart(run, lib.ArtifactThinking, lib.Part{Kind: "text", Text: synthStep})
						}
						run.mu.Unlock()
						synthAck := ackText
						if sawUnsettled {
							synthAck = ""
						}
						ackText = b.synthesizeCardAnswer(run, taskCtx, sessionID, turn, synthAck, relevant, parts)
					}
					break
				}
			}
		} else if taskCtx.Err() != nil {
			b.finalizeAPIError(run, taskCtx, taskCtx.Err(), turn)
			return "", false
		}

		if hasQueuedSteer(run) {
			break
		}

		select {
		case <-taskCtx.Done():
			b.finalizeAPIError(run, taskCtx, taskCtx.Err(), turn)
			return "", false
		case <-ticker.C:
		}
	}

	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state != stateRunning {
		return "", false
	}
	run.answerHeld = true
	return ackText, true
}

func (b *Bridge) synthesizeCardAnswer(run *taskRun, taskCtx context.Context, sessionID string, turn int, ackText string, cards []kanbanCardRow, parts []string) string {
	joined := strings.Join(parts, "\n\n")
	b.publishTurnAnswer(run, turn*cardTurnArtifactStride, cardResultsHeader+joined)

	if len(strings.TrimSpace(ackText)) >= cardAnswerSubstantiveChars && !isSilentAnswer(ackText) {
		return ackText
	}
	if len(cards) == 1 {
		return joined
	}
	if len(joined) > cardSynthesisInputCap {
		joined = joined[:cardSynthesisInputCap] + "\n\n[... further card results truncated for synthesis ...]"
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "All %d specialist card(s) you filed for this request have now completed. Their results are below. ", len(cards))
	sb.WriteString("IMPORTANT: this conversation has no chat thread, so the gateway has NOT posted these results anywhere; ")
	sb.WriteString("the user has seen nothing yet and your reply is the only answer they will get. [SILENT] is therefore not an ")
	sb.WriteString("acceptable reply here. ")
	sb.WriteString("Write the final answer to the user's original request as a synthesis: lead with the overall verdict, ")
	sb.WriteString("then a compact per-item table (one row per card/cluster) with status and the one key fact, then only the ")
	sb.WriteString("findings that need action with concrete next steps. Do not paste the raw reports; keep it under about ")
	sb.WriteString("400 words unless the user explicitly asked for full detail. Do not run more tools or file more cards.\n\n")
	for i, c := range cards {
		title := strings.TrimSpace(c.Title)
		if title == "" {
			title = c.ID
		}
		fmt.Fprintf(&sb, "### Card %d: %s (%s)\n%s\n\n", i+1, title, strings.TrimSpace(c.Status), strings.TrimSpace(parts[min(i, len(parts)-1)]))
	}
	synth, ok := b.apiSynthesisTurn(run, taskCtx, sessionID, sb.String(), turn)
	if !ok || isSilentAnswer(synth) {
		return joined
	}
	return synth
}

func isSilentAnswer(text string) bool {
	t := strings.TrimSpace(text)
	return t == "" || strings.EqualFold(t, silentSentinel) || strings.EqualFold(strings.Trim(t, "`*_ "), silentSentinel)
}

func (b *Bridge) apiSynthesisTurn(run *taskRun, taskCtx context.Context, sessionID, prompt string, turn int) (string, bool) {
	body, err := json.Marshal(apiChatRequest{
		Model:    b.cfg.APIModel,
		Messages: []apiChatMessage{{Role: "user", Content: prompt}},
	})
	if err != nil {
		return "", false
	}
	ctx, cancel := context.WithTimeout(taskCtx, cardSynthesisTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.cfg.APIURL, bytes.NewReader(body))
	if err != nil {
		return "", false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.cfg.APIKey)
	req.Header.Set(apiSessionKeyHeader, sessionID)
	req.Header.Set(apiSessionIDHeader, sessionID)
	req.Header.Set(apiIdempotencyHeader, fmt.Sprintf("%s/synth-%d", run.origin.TaskID, turn))

	resp, err := b.sendAPI(ctx, req, body)
	if err != nil {
		b.cfg.Logger.Warn("card synthesis turn failed; delivering raw card results", "task", run.origin.TaskID, "err", err)
		return "", false
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, apiResponseCap+1))
	if err != nil || resp.StatusCode < 200 || resp.StatusCode > 299 || len(raw) > apiResponseCap {
		b.cfg.Logger.Warn("card synthesis turn refused; delivering raw card results",
			"task", run.origin.TaskID, "status", resp.StatusCode, "err", err)
		return "", false
	}
	var out apiChatResponse
	if json.Unmarshal(raw, &out) != nil || len(out.Choices) == 0 || apiTurnFailed(&out) {
		return "", false
	}
	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state != stateRunning {
		return "", false
	}
	return out.Choices[0].Message.Content, true
}

func sanitizeKanbanTaskToken(cardID string) string {
	s := strings.ToLower(strings.TrimSpace(cardID))
	var sb strings.Builder
	sb.WriteString("task-card-")
	for _, r := range s {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9'):
			sb.WriteRune(r)
		case r == '-' || r == '_':
			sb.WriteByte('-')
		}
	}
	out := strings.Trim(sb.String(), "-")
	if out == "task-card" {
		return "task-card-unknown"
	}
	return out
}

func (b *Bridge) watchOutOfTurnKanbanCards(ctx context.Context) {
	dbPath := resolveKanbanDBPath()
	if dbPath == "" {
		return
	}
	pythonBin := resolvePythonBin()
	sinceUnix := time.Now().Unix() - kanbanOutOfTurnWindowSecs

	if initial, err := querySessionKanbanCards(ctx, pythonBin, dbPath, "*", sinceUnix, nil); err == nil {
		for _, c := range initial {
			if isKanbanStatusSettled(c.Status) {
				b.markKanbanCardDelivered(c.ID)
			}
		}
	}

	ticker := time.NewTicker(kanbanOutOfTurnInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		cards, err := querySessionKanbanCards(ctx, pythonBin, dbPath, "*", sinceUnix, nil)
		if err != nil {
			continue
		}
		for _, c := range cards {
			if !isKanbanStatusSettled(c.Status) || c.SessionID == "" {
				continue
			}
			if b.recordedRoute(c.SessionID).Platform != "" {
				b.markKanbanCardDelivered(c.ID)
				continue
			}
			if meta, ok := b.shouldDeliverOutOfTurnKanbanCard(c.ID, c.SessionID); ok {
				b.publishOutOfTurnKanbanCard(ctx, c, meta)
			}
		}
	}
}

func (b *Bridge) publishOutOfTurnKanbanCard(ctx context.Context, c kanbanCardRow, meta consoleSessionMeta) {
	if meta.ContextID == "" {
		return
	}
	synthTaskID := sanitizeKanbanTaskToken(c.ID)
	corrID := meta.CorrelationID
	if corrID == "" {
		corrID = synthTaskID
	}
	output := formatKanbanCardOutput(c)
	if output == "" {
		output = fmt.Sprintf("Kanban card `%s` (%s) finished with status `%s`.", c.ID, c.Title, c.Status)
	}
	subj := lib.TaskEventsSubject(b.cfg.Profile, synthTaskID)

	artPayload, err := json.Marshal(lib.ArtifactUpdate{
		TaskID:    synthTaskID,
		ContextID: meta.ContextID,
		Artifact: lib.Artifact{
			ArtifactID: "artifact-" + synthTaskID + "-" + lib.ArtifactResult,
			Name:       lib.ArtifactResult,
			Parts:      []lib.Part{{Kind: "text", Text: output}},
		},
		LastChunk: true,
	})
	if err == nil {
		if env, envErr := lib.NewArtifactUpdateEnvelope(b.from, synthTaskID, meta.ContextID, corrID, artPayload); envErr == nil {
			if len(meta.Authority) > 0 {
				env.Authority = meta.Authority
			}
			pubCtx, cancel := context.WithTimeout(ctx, activityPublishTimeout)
			_ = b.c.Publish(pubCtx, subj, env)
			cancel()
		}
	}

	termState := lib.StateCompleted
	switch strings.ToLower(strings.TrimSpace(c.Status)) {
	case "blocked":
		termState = lib.StateInputRequired
	case "failed", "gave_up":
		termState = lib.StateFailed
	case "canceled", "cancelled":
		termState = lib.StateCanceled
	}
	stPayload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    synthTaskID,
		ContextID: meta.ContextID,
		Status:    lib.TaskStatus{State: termState},
		Final:     termState.Terminal(),
	})
	if err == nil {
		if env, envErr := lib.NewStatusUpdateEnvelope(b.from, synthTaskID, meta.ContextID, corrID, stPayload); envErr == nil {
			if len(meta.Authority) > 0 {
				env.Authority = meta.Authority
			}
			pubCtx, cancel := context.WithTimeout(ctx, activityPublishTimeout)
			_ = b.c.Publish(pubCtx, subj, env)
			cancel()
		}
	}
}
