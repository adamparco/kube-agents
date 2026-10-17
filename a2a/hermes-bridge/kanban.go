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

	// Card synthesis (synthesizeCardAnswer).
	cardAnswerSubstantiveChars = 600             // a model answer at least this long already synthesized its cards
	cardSynthesisInputCap      = 120 << 10       // raw card text handed to the synthesis turn
	cardSynthesisTimeout       = 4 * time.Minute // one extra model turn; falls back on expiry
	cardTurnArtifactStride     = 1000            // keeps card-dump turn artifact ids clear of real turn numbers
	cardResultsHeader          = "Specialist card results (raw):\n\n"
	silentSentinel             = "[SILENT]"
)

const kanbanPollPyScript = `
import json, sqlite3, sys
db_path, session_id, since_ts = sys.argv[1], sys.argv[2], float(sys.argv[3])
tracked = [x for x in (sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] else []) if x]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
conn.row_factory = sqlite3.Row
cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
sess_col = "creator_session_id" if "creator_session_id" in cols else "session_id"
err_col = "last_failure_error" if "last_failure_error" in cols else "''"
assignee_col = "assignee" if "assignee" in cols else "''"
settled = ("done", "archived", "completed", "blocked", "failed", "gave_up", "canceled", "cancelled")
settled_sql = ",".join(repr(s) for s in settled)
if session_id == "*":
    rows = conn.execute(
        f"SELECT id, title, status, COALESCE({assignee_col}, '') AS assignee, COALESCE({sess_col}, '') AS session_id, COALESCE(result, '') AS result, COALESCE({err_col}, '') AS err "
        f"FROM tasks WHERE {sess_col} LIKE 'a2a-%' AND (created_at >= ? OR LOWER(COALESCE(status, '')) NOT IN ({settled_sql})) "
        f"ORDER BY created_at ASC, id ASC",
        (since_ts,),
    ).fetchall()
else:
    conds = ["created_at >= ?"]
    params = [session_id, since_ts]
    if tracked:
        conds.append(f"id IN ({','.join('?' for _ in tracked)})")
        params.extend(tracked)
    rows = conn.execute(
        f"SELECT id, title, status, COALESCE({assignee_col}, '') AS assignee, COALESCE({sess_col}, '') AS session_id, COALESCE(result, '') AS result, COALESCE({err_col}, '') AS err "
        f"FROM tasks WHERE {sess_col} = ? AND ({' OR '.join(conds)}) ORDER BY created_at ASC, id ASC",
        params,
    ).fetchall()

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
            run_row = conn.execute(
                f"SELECT COALESCE(summary, '') AS summary, COALESCE(error, '') AS error, COALESCE({ws_col}, '') AS worker_session_id, COALESCE({verd_col}, '') AS verdict, COALESCE({meta_col}, '') AS metadata "
                "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            if run_row:
                if not item["result"] and run_row["summary"]:
                    item["result"] = run_row["summary"]
                if not item["err"] and run_row["error"]:
                    item["err"] = run_row["error"]
                if run_row["worker_session_id"]:
                    item["worker_session_id"] = run_row["worker_session_id"]
                if run_row["verdict"]:
                    item["verdict"] = run_row["verdict"]
                if run_row["metadata"]:
                    try:
                        meta = json.loads(run_row["metadata"])
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
        except Exception:
            pass
    try:
        ev_row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'gave_up') ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        if ev_row and ev_row[0]:
            item["block_payload"] = ev_row[0]
    except Exception:
        pass
    out.append(item)
print(json.dumps(out))
`

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

const kanbanCancelSessionPyScript = `
import os, re, sqlite3, subprocess, sys, time

db_path, session_id = sys.argv[1], sys.argv[2]
settled = ("done", "archived", "completed", "blocked", "failed", "gave_up", "canceled", "cancelled")
settled_sql = ",".join(f"'{s}'" for s in settled)
now = int(time.time())
card_ids = []
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(tasks)")}
        sess_col = "creator_session_id" if "creator_session_id" in cols else ("session_id" if "session_id" in cols else ("origin_session_id" if "origin_session_id" in cols else "source_session_id"))
        rows = conn.execute(
            f"SELECT id FROM tasks WHERE {sess_col} = ? AND LOWER(COALESCE(status, '')) NOT IN ({settled_sql})",
            (session_id,),
        ).fetchall()
        card_ids = [r[0] for r in rows if r and r[0]]
        if card_ids and "parent_id" in cols:
            ph = ",".join("?" for _ in card_ids)
            child_rows = conn.execute(
                f"SELECT id FROM tasks WHERE parent_id IN ({ph}) AND LOWER(COALESCE(status, '')) NOT IN ({settled_sql})",
                card_ids,
            ).fetchall()
            for cr in child_rows:
                if cr and cr[0] and cr[0] not in card_ids:
                    card_ids.append(cr[0])
        if card_ids:
            ph = ",".join("?" for _ in card_ids)
            err_col = "last_failure_error" if "last_failure_error" in cols else ("error" if "error" in cols else ("err" if "err" in cols else None))
            upd_cols = ["status = 'canceled'"]
            upd_params = []
            if "updated_at" in cols:
                upd_cols.append("updated_at = ?")
                upd_params.append(now)
            if err_col:
                upd_cols.append(f"{err_col} = 'Canceled by user (A2A task stopped)'")
            conn.execute(f"UPDATE tasks SET {', '.join(upd_cols)} WHERE id IN ({ph})", upd_params + card_ids)
            conn.commit()
        conn.close()
    except Exception:
        pass

for cid in card_ids:
    if not re.fullmatch(r"t_[A-Za-z0-9_-]{1,64}", str(cid)):
        continue
    try:
        subprocess.run(["pkill", "-TERM", "-f", f"/kanban/workspaces/{cid}"], check=False, timeout=2)
    except Exception:
        pass
`

func (b *Bridge) pruneKanbanMapsLocked() {
	if len(b.kanbanDeliveredIDs) > maxTrackedKanbanEntries {
		clear(b.kanbanDeliveredIDs)
	}
	if len(b.kanbanCanceledSessions) > maxTrackedKanbanEntries {
		clear(b.kanbanCanceledSessions)
	}
	if len(b.sessionEpochs) > maxTrackedKanbanEntries {
		clear(b.sessionEpochs)
	}
	if len(b.consoleSessions) > maxTrackedKanbanEntries {
		clear(b.consoleSessions)
	}
}

func (b *Bridge) sessionIDForContext(contextID string) string {
	base := apiSessionID(contextID)
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	if epoch := b.sessionEpochs[contextID]; epoch > 0 {
		return fmt.Sprintf("%s-e%d", base, epoch)
	}
	return base
}

func (b *Bridge) markSessionCanceled(contextID, sessionID string) {
	b.kanbanMu.Lock()
	b.pruneKanbanMapsLocked()
	if b.kanbanCanceledSessions == nil {
		b.kanbanCanceledSessions = make(map[string]bool)
	}
	b.kanbanCanceledSessions[sessionID] = true
	if b.sessionEpochs == nil {
		b.sessionEpochs = make(map[string]int)
	}
	if contextID != "" {
		b.sessionEpochs[contextID]++
	}
	b.kanbanMu.Unlock()

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
		_ = exec.CommandContext(ctx, pythonBin, "-c", kanbanCancelSessionPyScript, dbPath, sessionID).Run()
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
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	b.pruneKanbanMapsLocked()
	if b.consoleSessions == nil {
		b.consoleSessions = make(map[string]consoleSessionMeta)
	}
	b.consoleSessions[sessionID] = consoleSessionMeta{
		ContextID:     origin.ContextID,
		CorrelationID: origin.CorrelationID,
		Authority:     append(json.RawMessage(nil), origin.Authority...),
	}
}

func (b *Bridge) beginKanbanWait(sessionID string) {
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	if b.kanbanActiveWaits == nil {
		b.kanbanActiveWaits = make(map[string]int)
	}
	b.kanbanActiveWaits[sessionID]++
}

func (b *Bridge) endKanbanWait(sessionID string) {
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	if b.kanbanActiveWaits[sessionID] > 0 {
		b.kanbanActiveWaits[sessionID]--
		if b.kanbanActiveWaits[sessionID] == 0 {
			delete(b.kanbanActiveWaits, sessionID)
		}
	}
}

func (b *Bridge) markKanbanCardDelivered(cardID string) {
	if cardID == "" {
		return
	}
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	b.pruneKanbanMapsLocked()
	if b.kanbanDeliveredIDs == nil {
		b.kanbanDeliveredIDs = make(map[string]bool)
	}
	b.kanbanDeliveredIDs[cardID] = true
	delete(b.kanbanPendingSettle, cardID)
}

func (b *Bridge) isKanbanCardDelivered(cardID string) bool {
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	return b.kanbanDeliveredIDs[cardID]
}

func (b *Bridge) shouldDeliverOutOfTurnKanbanCard(cardID, sessionID string) (consoleSessionMeta, bool) {
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
	if b.kanbanDeliveredIDs[cardID] || b.kanbanCanceledSessions[sessionID] {
		return consoleSessionMeta{}, false
	}
	if b.kanbanActiveWaits[sessionID] > 0 {
		delete(b.kanbanPendingSettle, cardID)
		return consoleSessionMeta{}, false
	}
	if b.kanbanPendingSettle == nil {
		b.kanbanPendingSettle = make(map[string]int)
	}
	b.kanbanPendingSettle[cardID]++
	if b.kanbanPendingSettle[cardID] < 2 {
		return consoleSessionMeta{}, false
	}
	b.pruneKanbanMapsLocked()
	if b.kanbanDeliveredIDs == nil {
		b.kanbanDeliveredIDs = make(map[string]bool)
	}
	b.kanbanDeliveredIDs[cardID] = true
	delete(b.kanbanPendingSettle, cardID)
	meta := b.consoleSessions[sessionID]
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
	var candidates []string
	if v := strings.TrimSpace(os.Getenv("HERMES_KANBAN_DB")); v != "" {
		candidates = append(candidates, v)
	}
	if v := strings.TrimSpace(os.Getenv("HERMES_HOME")); v != "" {
		candidates = append(candidates, filepath.Join(v, "kanban.db"))
	}
	if v := strings.TrimSpace(os.Getenv("PLATFORM_AGENT_HOME")); v != "" {
		candidates = append(candidates, filepath.Join(v, "kanban.db"))
	}
	candidates = append(candidates, "/opt/data/kanban.db")
	for _, p := range candidates {
		if info, err := os.Stat(p); err == nil && !info.IsDir() {
			return p
		}
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
	if errTxt := strings.TrimSpace(c.Err); errTxt != "" {
		return errTxt
	}
	return ""
}

func querySessionKanbanCards(ctx context.Context, pythonBin, dbPath, sessionID string, sinceUnix int64, trackedIDs []string) ([]kanbanCardRow, error) {
	qCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(qCtx, pythonBin, "-c", kanbanPollPyScript, dbPath, sessionID, strconv.FormatInt(sinceUnix, 10), strings.Join(trackedIDs, ","))
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

// waitForKanbanCards waits for any Kanban card(s) filed or active during the
// turn on a conversation with no chat.notify route (such as console:). While
// waiting, waitingCard is set on act so the Kanban worker's tool-call and
// thinking webhook deliveries stream onto this A2A task.
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
					runningCount := totalCount - settledCount
					progText := fmt.Sprintf("Kanban specialist checks: %d/%d completed (%d running) — latest: %s (%s)", settledCount, totalCount, runningCount, shortTitle, verdict)
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

// synthesizeCardAnswer turns the settled cards of a console turn into the turn's answer.
func (b *Bridge) synthesizeCardAnswer(run *taskRun, taskCtx context.Context, sessionID string, turn int, ackText string, cards []kanbanCardRow, parts []string) string {
	joined := strings.Join(parts, "\n\n")
	// The raw card results are the evidence; keep them on the stream regardless of what the
	// answer becomes.
	b.publishTurnAnswer(run, turn*cardTurnArtifactStride, cardResultsHeader+joined)

	if len(strings.TrimSpace(ackText)) >= cardAnswerSubstantiveChars && !isSilentAnswer(ackText) {
		// The model waited for its cards inside its own turn and already synthesized; its answer stands.
		return ackText
	}
	if len(cards) == 1 {
		// One card's report is already a complete answer; a synthesis turn would only restate it.
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

// isSilentAnswer reports whether text is the chat agents' "nothing to say" sentinel (or empty).
func isSilentAnswer(text string) bool {
	t := strings.TrimSpace(text)
	return t == "" || strings.EqualFold(t, silentSentinel) || strings.EqualFold(strings.Trim(t, "`*_ "), silentSentinel)
}

// apiSynthesisTurn posts one extra, non-steer turn to the session and returns its answer.
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

// watchOutOfTurnKanbanCards polls kanban.db for cards belonging to a2a-*
// sessions that settle while no in-flight A2A task is waiting on them (for
// example, after a pod restart or client timeout) and publishes their final
// result onto a2a.tasks.<profile>.task-card-<id>.events so console/MCP clients
// receive the completion asynchronously.
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
			meta, ok := b.shouldDeliverOutOfTurnKanbanCard(c.ID, c.SessionID)
			if !ok {
				continue
			}
			b.publishOutOfTurnKanbanCard(ctx, c, meta)
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
		Status: lib.TaskStatus{
			State: termState,
		},
		Final: termState.Terminal(),
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
