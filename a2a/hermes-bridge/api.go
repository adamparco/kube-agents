package hermesbridge

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The API executor: a task is a turn in the conversation's Hermes session,
// and one more turn in it for each follow-up the task queues.
//
// Instead of a cold `hermes chat -Q` per task, the bridge POSTs the task's
// text to the Hermes API server in the same pod, which runs it under the
// gateway's own profile (default on a stock install), the persona that answers the same message on
// the chat path, delegating through kanban. Two headers make the turn part
// of a conversation rather than a one-off: X-Hermes-Session-Key, the
// long-term-memory scope, and X-Hermes-Session-Id, the session whose history
// the server loads from its own store before the turn and appends to after.
// Both are derived from the task's contextId, which the gateway mints once
// per backend conversation, so every task in a thread lands in one session
// and the second task sees the first's turns. Nothing in Hermes changes.
//
// The subprocess executor stays as a fallback (Config.Executor); what this
// one does not do, by design of the stopgap it is: steer a running turn (a
// follow-up waits for the turn to end and runs as the next turn in the same
// session; bridge.go, queueSteer). A kanban card's completion never reaches
// the A2A task (the API server has no push channel); it goes back to the
// conversation through the gateway's chat.notify route instead, on the route
// this executor records before a task's first turn (route.go). Both are
// named in a2a/docs/hermes-bridge.md.
const (
	// ExecutorAPI runs a task as a turn in the conversation's Hermes session
	// through the pod's API server. The daemon's default.
	ExecutorAPI = "api"
	// ExecutorCLI runs a task as its own `hermes chat -Q` subprocess, the
	// original executor and Config's zero value; a fresh session per task.
	ExecutorCLI = "cli"
	// DefaultAPIURL is the Hermes API server's chat-completions endpoint in
	// the pod: loopback, the port hermes's api_server binds by default.
	DefaultAPIURL = "http://127.0.0.1:8642/v1/chat/completions"
	// DefaultAPIModel is the model name a request carries. The pod's API
	// server serves one model under its API_SERVER_MODEL_NAME, the inference
	// gateway's; the name routes, it does not pick.
	DefaultAPIModel = "model-default"

	// apiSessionIDPrefix starts the session id derived from a contextId, so
	// the sessions the bridge opened are told apart in the profile's store
	// from the chat platforms' and the CLI's.
	apiSessionIDPrefix = "a2a-"
	// apiHashedSessionPrefix follows apiSessionIDPrefix when the contextId
	// is not usable verbatim (apiSessionID). It marks the id as hashed for a
	// reader; it does not partition the namespace, since a contextId of
	// "h-" plus 32 hex is itself verbatim-safe. Nothing rests on it: a
	// sender who can name a session's contextId can already send into it.
	apiHashedSessionPrefix = "h-"
	// apiContextIDMaxLen is the longest contextId used verbatim. The
	// gateway's are "ctx-" plus 32 hex; the server caps the header too.
	apiContextIDMaxLen = 128
	// apiHashedSessionHexLen is how much of the SHA-256 a hashed id keeps:
	// 128 bits, past any collision two conversations could stumble into.
	apiHashedSessionHexLen = 32
	// apiSessionKeyHeader and apiSessionIDHeader are the API server's two
	// session headers (gateway/platforms/api_server.py); both are echoed on
	// the response, and both require the server's API key to be configured.
	apiSessionKeyHeader = "X-Hermes-Session-Key"
	apiSessionIDHeader  = "X-Hermes-Session-Id"
	// apiIdempotencyHeader is the server's replay guard: a request retried
	// with the same key returns the first run's answer instead of running
	// the turn again. Each turn has its own key (apiTurnKey): the task id for
	// the opening turn, so a bridge restart that redelivers a task does not
	// run it twice in the session, and "<taskId>/<envelopeId>" for a
	// follow-up's, so it never replays the opening answer.
	apiIdempotencyHeader = "Idempotency-Key"
	// apiRateLimitedStatus is the status the server answers when it is
	// already running its cap of concurrent turns
	// (gateway.api_server.max_concurrent_runs), before the turn starts. It
	// maps to the reason token of the CLI's EX_TEMPFAIL exit, so the eval
	// harness classes it as infrastructure. A provider's own rate limit is
	// not this: the turn runs and fails, and is read from
	// apiFailureReasonHeader.
	apiRateLimitedStatus = http.StatusTooManyRequests
	// apiFailureReasonHeader names the failure_reason Hermes classified a
	// failed turn under; the image's api_failure_reason_header patch adds it
	// to both the 200 and the 502 answer. apiRateLimitedReasons are the
	// reasons the CLI exits 75 on, so a turn that gave up on the provider is
	// hermes-rate-limited on either executor.
	apiFailureReasonHeader = "X-Hermes-Failure-Reason"
	// apiBodyTailBytes bounds the error body quoted in a failed terminal.
	apiBodyTailBytes = 2048
	// apiResponseCap bounds a successful response body read: an answer is
	// text, and anything past this is not one. The read takes one byte more,
	// so a body over the cap is refused as hermes-api-oversize rather than
	// cut at the cap and misread as hermes-api-unreadable.
	apiResponseCap = 8 << 20
	// apiURLSchemeHTTP and apiURLSchemeHTTPS are the schemes the API URL
	// may carry; the client cannot send to anything else.
	apiURLSchemeHTTP  = "http"
	apiURLSchemeHTTPS = "https"
	// DefaultAPIConnectRetry and apiConnectRetryInterval pace the retry of a
	// refused connection: the sidecar and the agent container start
	// together, and the bridge can be consuming before hermes's API server
	// listens. A refused connection never reached the server, so retrying it
	// cannot run a turn twice. Past the window the server is not starting,
	// it is absent, and the task ends hermes-api-unreachable.
	DefaultAPIConnectRetry  = 2 * time.Minute
	apiConnectRetryInterval = time.Second
)

// errWaitingForTurn is the error a task whose context ended while it waited
// for its session's previous turn carries into finalizeAPIError.
var errWaitingForTurn = errors.New("waiting for the session's previous turn")

// errNeverConnected is what sendAPI wraps around an error when no attempt
// got a connection: no request reached the server, so a deadline that ends
// it is not the turn's.
var errNeverConnected = errors.New("no connection to the hermes API server")

// apiRateLimitedReasons is the set apiFailureReasonHeader is checked against:
// the reasons apply_quiet_rate_limit_exit.py exits 75 on.
var apiRateLimitedReasons = map[string]bool{"rate_limit": true, "billing": true}

// apiSafeContextID is what a contextId may hold to be used verbatim in a
// session id: the server interpolates the id into a filename and refuses a
// separator or "..", and the bus does not vouch for a contextId's shape.
var apiSafeContextID = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// apiSessionID is the Hermes session a conversation's tasks share: the
// contextId under apiSessionIDPrefix when it is path-safe, else a hash of
// it, so a hostile or odd contextId still maps to one stable session and
// never to a path. Every task has a contextId: the envelope refuses one
// without.
func apiSessionID(contextID string) string {
	if len(contextID) <= apiContextIDMaxLen && apiSafeContextID.MatchString(contextID) {
		return apiSessionIDPrefix + contextID
	}
	sum := sha256.Sum256([]byte(contextID))
	return apiSessionIDPrefix + apiHashedSessionPrefix + hex.EncodeToString(sum[:])[:apiHashedSessionHexLen]
}

// sessionTurns serializes turns per Hermes session. Two tasks in one
// conversation can be in flight at once (Concurrency > 1), and two
// concurrent turns in one session would each load the history without the
// other's turn and append in a race; the second waits for the first, as a
// second chat message waits in a chat platform's session.
type sessionTurns struct {
	mu    sync.Mutex
	slots map[string]*sessionSlot
}

type sessionSlot struct {
	ch   chan struct{} // capacity 1: held while a turn is in the session
	refs int           // tasks holding or waiting on ch
}

// acquire waits for the session's turn or ctx's end. The release it returns
// is non-nil exactly when the turn was taken.
func (t *sessionTurns) acquire(ctx context.Context, sessionID string) func() {
	t.mu.Lock()
	if t.slots == nil {
		t.slots = make(map[string]*sessionSlot)
	}
	s := t.slots[sessionID]
	if s == nil {
		s = &sessionSlot{ch: make(chan struct{}, 1)}
		t.slots[sessionID] = s
	}
	s.refs++
	t.mu.Unlock()
	drop := func() {
		t.mu.Lock()
		s.refs--
		if s.refs == 0 {
			delete(t.slots, sessionID)
		}
		t.mu.Unlock()
	}
	select {
	case s.ch <- struct{}{}:
		return func() {
			<-s.ch
			drop()
		}
	case <-ctx.Done():
		drop()
		return nil
	}
}

// apiChatRequest is the chat-completions request body, the subset the
// server reads for a plain turn.
type apiChatRequest struct {
	Model    string           `json:"model"`
	Messages []apiChatMessage `json:"messages"`
	Stream   bool             `json:"stream"`
}

type apiChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// apiChatResponse is the subset of the chat-completions response the bridge
// reads: the first choice's text, and the server's own verdict on the turn.
// A turn that failed with no text comes back as an OpenAI error envelope
// with a non-2xx status, read as text; one that failed with text (a
// provider error, a rate limit the retries did not outlast, the failure
// summary Hermes writes for either) comes back 200 with Hermes.Failed set.
type apiChatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Hermes *struct {
		Failed bool   `json:"failed"`
		Error  string `json:"error"`
	} `json:"hermes"`
}

// apiTurnFailed reports whether the server marked the turn failed, the case
// in which `hermes chat -Q` exits non-zero; a partial or truncated turn that
// did not fail completes, as it does on the subprocess path.
func apiTurnFailed(out *apiChatResponse) bool {
	return out.Hermes != nil && out.Hermes.Failed
}

// runTaskAPI is runTask for ExecutorAPI. The lifecycle is the subprocess
// path's: working when the first turn starts, one terminal from this
// goroutine, cancel and the deadline end the request rather than a process
// group, and shutdown names itself. A follow-up queued during a turn runs as
// the next turn in the same session, under the same session slot, after the
// turn before it has answered.
func (b *Bridge) runTaskAPI(ctx context.Context, run *taskRun) {
	taskID := run.origin.TaskID
	prompt, ok := promptFromMessage(run.origin.Payload)
	if !ok {
		b.finalize(run, lib.StateRejected,
			"reason: no-text-parts - the submission message carries nothing hermes can be asked", nil)
		return
	}
	sessionID := apiSessionID(run.origin.ContextID)
	// The task's own context: the deadline bounds every turn together;
	// cancel and shutdown end whichever request is in flight. Not the
	// consumer's context, which a shutdown cancels before shutdownTasks can
	// name the cause. It stays in cancelReq for the whole task, so a cancel
	// between turns still ends the next one's request.
	taskCtx, cancelTask := context.WithTimeout(context.Background(), b.cfg.TaskDeadline)
	defer cancelTask()
	// The deadline as a flag too, as the cli timer sets it: nextSteer reads
	// it between turns, so no follow-up is picked once the deadline is up.
	stopDeadline := context.AfterFunc(taskCtx, func() {
		if errors.Is(taskCtx.Err(), context.DeadlineExceeded) {
			run.deadlineHit.Store(true)
		}
	})
	defer stopDeadline()
	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return
	}
	run.cancelReq = cancelTask
	if run.canceled.Load() {
		cancelTask()
	}
	run.mu.Unlock()
	defer func() { run.mu.Lock(); run.cancelReq = nil; run.mu.Unlock() }()

	// A task behind its session's previous turn has not started: it stays
	// submitted, with no heartbeat, until the turn is its own, so nothing
	// reads a wait as a run.
	release := b.turns.acquire(taskCtx, sessionID)
	if release == nil {
		b.finalizeAPIError(run, taskCtx, errWaitingForTurn, 1)
		return
	}
	defer release() // every turn of the task runs under this one slot
	if err := b.publishWorking(ctx, run); errors.Is(err, errRunEnded) {
		return // canceled or shut down first; its finalize wrote the terminal
	} else if err != nil {
		b.cfg.Logger.Error("working publish failed", "task", taskID, "err", err)
		b.finalize(run, lib.StateFailed, "reason: bus-publish-failed at working", nil)
		return
	}

	// Recorded before the first turn, so a card any turn files finds it;
	// every turn of the task is the same session and conversation. Best
	// effort: the task runs either way, and a failure is said in the answer.
	routeLost := false
	routeErr := b.recordRoute(taskCtx, sessionID, run.origin)
	if routeErr != nil && !errors.Is(routeErr, errNoChatConversation) && !errors.Is(routeErr, errRouteDisabled) {
		b.cfg.Logger.Warn("conversation route not recorded; a card this task files cannot report back",
			"task", taskID, "session", sessionID, "err", routeErr)
		routeLost = true
	}
	// Only a recorded route lets a card's post reach the conversation, so only
	// then may the terminal keep the ask for those posts to settle (cardsMark).
	run.routeRecorded.Store(routeErr == nil)
	if routeErr != nil {
		b.rememberConsoleSession(sessionID, run.origin)
	}

	// The door's side of this task: attributed by the session id the hook
	// payload carries, signed with the pod's shared secret, and only while
	// this task holds the session's turn - every turn of it.
	act := newSessionActivityState(sessionID, b.apiTraced())
	act.inTurn.Store(true)
	defer act.inTurn.Store(false)
	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return
	}
	run.act.Store(act)
	go b.runActivity(run)
	run.mu.Unlock()

	var steer *lib.Envelope // the follow-up this turn runs; nil on turn 1
	var prev string         // the turn before's answer, published as a turn artifact
	for turn := 1; ; turn++ {
		turnStartUnix := time.Now().Unix() - 2
		text, ok := b.apiTurn(run, taskCtx, sessionID, prompt, steer, turn, prev)
		if !ok {
			return
		}
		if !run.routeRecorded.Load() {
			text, ok = b.waitForKanbanCards(run, taskCtx, act, sessionID, turnStartUnix, turn, text)
			if !ok {
				return
			}
		}
		next, nextPrompt := b.runnableSteer(ctx, run)
		if next == nil {
			// A canceled task whose turn finished anyway won the race:
			// completed wins, per the payload spec's cancel mapping, as on
			// the subprocess path.
			if routeLost {
				text += routeLostNote
			}
			b.finalize(run, lib.StateCompleted, "", &text)
			return
		}
		if !b.publishTurnAnswer(run, turn, text) {
			return
		}
		steer, prompt, prev = next, nextPrompt, text
	}
}

// apiTurnKey is a turn's Idempotency-Key. The opening turn keeps the task
// id, which is what a redelivered submission was always keyed on; a
// follow-up adds its own envelope id, so it never replays the opening
// answer and a redelivered follow-up maps to its own first run.
func apiTurnKey(taskID string, steer *lib.Envelope) string {
	if steer == nil {
		return taskID
	}
	return taskID + "/" + steer.EnvelopeID
}

// apiTurn posts one turn to the session on taskCtx and returns its answer.
// steer is the follow-up the turn runs, nil on turn 1, and prev the answer
// of the turn before it; the follow-up leaves the queue in the critical
// section that checks the task is still to run, so a cancel, deadline or
// shutdown that lands after that check finds the request's context in
// cancelReq and ends it. false means the task is final: it already was, or
// this finalized it - including a follow-up's turn that found the task
// canceled, past its deadline or the bridge stopping, which sends nothing
// and ends the task with prev still attached as the result
// (finalizeTurnNotSent).
func (b *Bridge) apiTurn(run *taskRun, taskCtx context.Context, sessionID, prompt string, steer *lib.Envelope, turn int, prev string) (string, bool) {
	body, err := json.Marshal(apiChatRequest{
		Model:    b.cfg.APIModel,
		Messages: []apiChatMessage{{Role: "user", Content: prompt}},
	})
	if err != nil {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: request-encode-failed - %v", err), nil)
		return "", false
	}
	req, err := http.NewRequestWithContext(taskCtx, http.MethodPost, b.cfg.APIURL, bytes.NewReader(body))
	if err != nil {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: request-build-failed - %v", err), nil)
		return "", false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.cfg.APIKey)
	req.Header.Set(apiSessionKeyHeader, sessionID)
	req.Header.Set(apiSessionIDHeader, sessionID)
	req.Header.Set(apiIdempotencyHeader, apiTurnKey(run.origin.TaskID, steer))

	if steer != nil {
		run.mu.Lock()
		if run.state != stateRunning {
			// Finalized first; it refused this follow-up with the rest.
			run.mu.Unlock()
			return "", false
		}
		if run.canceled.Load() || run.deadlineHit.Load() || taskCtx.Err() != nil || b.closing.Load() {
			// Checked under the lock the cancel and shutdown take before
			// they end cancelReq, after each has stored its flag: one that
			// lands after this check ends the request below.
			run.mu.Unlock()
			b.finalizeTurnNotSent(run, prev)
			return "", false
		}
		if !takeSteerLocked(run, steer) {
			// A finalize has taken the queue (and refused it) and is on its
			// way to the terminal. Unreachable while every finalizer sets a
			// flag checked above; a request here would contradict its notice.
			run.mu.Unlock()
			b.cfg.Logger.Error("follow-up gone from the queue head before its turn; not sending",
				"task", run.origin.TaskID, "envelope", steer.EnvelopeID)
			return "", false
		}
		// The last answer is on the stream as a turn artifact; from here
		// a shutdown ends this request, and its terminal names the turn.
		run.answerHeld = false
		run.turn = turn
		run.mu.Unlock()
	}

	resp, err := b.sendAPI(taskCtx, req, body)
	if err != nil {
		b.finalizeAPIError(run, taskCtx, err, turn)
		return "", false
	}
	defer resp.Body.Close()
	// taskCtx stays cancelable through the body read: the server can send
	// its headers before the body, and a cancel or shutdown in between must
	// still end the request.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, apiResponseCap+1))
	if err != nil && taskCtx.Err() != nil {
		b.finalizeAPIError(run, taskCtx, err, turn)
		return "", false
	}
	if err != nil {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-read-failed - %v%s", err, turnNote(turn)), nil)
		return "", false
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		reason := "hermes-api-failed"
		switch {
		case resp.StatusCode == apiRateLimitedStatus || apiRateLimitedReasons[resp.Header.Get(apiFailureReasonHeader)]:
			reason = "hermes-rate-limited"
		case resp.StatusCode >= http.StatusBadRequest && resp.StatusCode < http.StatusInternalServerError:
			// The server refuses a request in 4xx before an agent runs
			// (the key, the session headers, the profile, the body); a
			// turn that ran and failed answers 5xx.
			reason = "hermes-api-refused"
		}
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: %s - HTTP %d; session: %s%s; body tail: %s",
			reason, resp.StatusCode, sessionID, turnNote(turn), tail(string(raw), apiBodyTailBytes)), nil)
		return "", false
	}
	if len(raw) > apiResponseCap {
		// Refused, never truncated: a cut answer would fail to parse and
		// read as a protocol fault, and a shortened one is worse than a
		// loud failure.
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-oversize - HTTP %d; session: %s%s; "+
			"the response body is over the %d-byte limit (apiResponseCap in api.go); the answer was refused rather than truncated",
			resp.StatusCode, sessionID, turnNote(turn), apiResponseCap), nil)
		return "", false
	}
	var out apiChatResponse
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Choices) == 0 {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreadable - HTTP %d; session: %s%s; body tail: %s",
			resp.StatusCode, sessionID, turnNote(turn), tail(string(raw), apiBodyTailBytes)), nil)
		return "", false
	}
	if apiTurnFailed(&out) {
		reason := "hermes-api-failed"
		if apiRateLimitedReasons[resp.Header.Get(apiFailureReasonHeader)] {
			reason = "hermes-rate-limited"
		}
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: %s - HTTP %d, turn failed; session: %s%s; error: %s",
			reason, resp.StatusCode, sessionID, turnNote(turn), tail(out.Hermes.Error, apiBodyTailBytes)), nil)
		return "", false
	}
	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state != stateRunning {
		return "", false // finalized while the answer was read
	}
	// Held until it is on the stream: shutdownTasks leaves the run to this
	// worker from here (answerHeld).
	run.answerHeld = true
	return out.Choices[0].Message.Content, true
}

// sendAPI sends req, retrying a refused connection every
// apiConnectRetryInterval for up to Config.APIConnectRetry while ctx lasts.
// A response of any status is returned as it is; an error is wrapped in
// errNeverConnected when no attempt got a connection, so no request was
// sent.
func (b *Bridge) sendAPI(ctx context.Context, req *http.Request, body []byte) (*http.Response, error) {
	var connected atomic.Bool
	ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
		GotConn: func(httptrace.GotConnInfo) { connected.Store(true) },
	})
	req = req.WithContext(ctx)
	neverConnected := func(err error) error {
		if connected.Load() {
			return err
		}
		return fmt.Errorf("%w: %w", errNeverConnected, err)
	}
	giveUp := time.Now().Add(b.cfg.APIConnectRetry)
	for {
		resp, err := b.apiClient.Do(req)
		if err == nil {
			return resp, nil
		}
		if !errors.Is(err, syscall.ECONNREFUSED) || time.Now().After(giveUp) {
			return nil, neverConnected(err)
		}
		select {
		case <-ctx.Done():
			return nil, neverConnected(err)
		case <-time.After(apiConnectRetryInterval):
		}
		req = req.Clone(ctx)
		req.Body = io.NopCloser(bytes.NewReader(body))
	}
}

// finalizeTurnNotSent ends a task whose next follow-up found it canceled,
// past its deadline or the bridge stopping before its request was sent. The
// turn before's answer is already on the stream as a turn artifact, but a
// reader of the result would not find it, so it is attached as the result
// too, ahead of the terminal the cause calls for. The attached result is a
// copy (finalizeCopy): if it fails to publish, the terminal keeps the
// cause's state, so a cancel still ends canceled. The follow-up never ran,
// so the terminal does not name its turn.
func (b *Bridge) finalizeTurnNotSent(run *taskRun, answer string) {
	switch {
	case run.canceled.Load():
		b.finalizeCopy(run, lib.StateCanceled, "reason: canceled-by-request", answer)
	case b.closing.Load():
		b.finalizeCopy(run, lib.StateFailed, shutdownReason, answer)
	default:
		// Only the deadline is left: it passed between two turns, after
		// the last answer went out as a turn artifact.
		b.finalizeCopy(run, lib.StateFailed,
			fmt.Sprintf("reason: deadline-exceeded - the task deadline %s passed before the next turn; no request was sent", b.cfg.TaskDeadline), answer)
	}
}

// finalizeAPIError ends a task whose request (or wait for its session's
// turn) ended without a response, naming the cause: the cancel, the
// shutdown and the deadline each end reqCtx, so they are read first. A
// follow-up turn (turn ≥ 2) that started and ended here is named, as
// apiTurn's in-band failures name it.
func (b *Bridge) finalizeAPIError(run *taskRun, reqCtx context.Context, err error, turn int) {
	switch {
	case run.canceled.Load() && errors.Is(err, errWaitingForTurn):
		// Nothing was sent: the task was still waiting for its turn, the
		// API executor's queue.
		b.finalize(run, lib.StateCanceled, canceledBeforeStartReason, nil)
	case run.canceled.Load():
		b.finalize(run, lib.StateCanceled, "reason: canceled-by-request", nil)
	case b.closing.Load():
		b.finalize(run, lib.StateFailed, shutdownReason+turnNote(turn), nil)
	case reqCtx.Err() == context.DeadlineExceeded && errors.Is(err, errWaitingForTurn):
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: session-busy - waited %s for the session's previous turn; no request was sent", b.cfg.TaskDeadline), nil)
	case reqCtx.Err() == context.DeadlineExceeded && errors.Is(err, errNeverConnected):
		// Infrastructure, as the refused connection past the retry window
		// is: the deadline ended a wait for a server that never listened.
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreachable - task deadline %s ended before the server accepted a connection; no request was sent: %v%s", b.cfg.TaskDeadline, err, turnNote(turn)), nil)
	case reqCtx.Err() == context.DeadlineExceeded:
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: deadline-exceeded - request ended after %s%s", b.cfg.TaskDeadline, turnNote(turn)), nil)
	default:
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreachable - %v%s", err, turnNote(turn)), nil)
	}
}

// newAPIClient is the HTTP client the API executor uses: no client timeout
// (the request context carries the task deadline), and no proxy, since the
// server is loopback.
func newAPIClient() *http.Client {
	return &http.Client{Transport: &http.Transport{Proxy: nil, DisableKeepAlives: false}}
}

// apiExecutorValid reports whether cfg names a usable API executor: the
// name, a URL and the key the server needs before it honours the session
// headers. The daemon picks this executor with no key only when told to
// (BRIDGE_EXECUTOR=api), so an empty key here is a misdeclared sidecar,
// refused at start rather than one 401 per task.
func apiExecutorValid(cfg *Config) error {
	if strings.TrimSpace(cfg.APIKey) == "" {
		return fmt.Errorf("executor %q needs APIKey (the pod's API_SERVER_KEY): the API server refuses the session headers without it", ExecutorAPI)
	}
	u, err := url.ParseRequestURI(cfg.APIURL)
	if err != nil {
		return fmt.Errorf("executor %q: APIURL %q: %w", ExecutorAPI, cfg.APIURL, err)
	}
	// ParseRequestURI reads "localhost:8642/v1" as scheme "localhost", so
	// the scheme and host are what refuse a URL with no scheme.
	if (u.Scheme != apiURLSchemeHTTP && u.Scheme != apiURLSchemeHTTPS) || u.Host == "" {
		return fmt.Errorf("executor %q: APIURL %q: want an %s:// or %s:// URL with a host", ExecutorAPI, cfg.APIURL, apiURLSchemeHTTP, apiURLSchemeHTTPS)
	}
	return nil
}

const (
	kanbanPollInterval        = time.Second
	kanbanOutOfTurnInterval   = 2 * time.Second
	kanbanMaxEmptyPolls       = 3
	kanbanOutOfTurnWindowSecs = 7200
	hermesVenvPythonPath      = "/opt/hermes/.venv/bin/python3"

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
settled = ("done", "archived", "completed", "blocked", "failed", "gave_up", "canceled", "cancelled")
settled_sql = ",".join(repr(s) for s in settled)
if session_id == "*":
    rows = conn.execute(
        f"SELECT id, title, status, COALESCE({sess_col}, '') AS session_id, COALESCE(result, '') AS result, COALESCE({err_col}, '') AS err "
        f"FROM tasks WHERE {sess_col} LIKE 'a2a-%' AND (created_at >= ? OR LOWER(COALESCE(status, '')) NOT IN ({settled_sql})) "
        f"ORDER BY created_at ASC, id ASC",
        (since_ts,),
    ).fetchall()
else:
    conds = ["created_at >= ?", f"LOWER(COALESCE(status, '')) NOT IN ({settled_sql})"]
    params = [session_id, since_ts]
    if tracked:
        conds.append(f"id IN ({','.join('?' for _ in tracked)})")
        params.extend(tracked)
    rows = conn.execute(
        f"SELECT id, title, status, COALESCE({sess_col}, '') AS session_id, COALESCE(result, '') AS result, COALESCE({err_col}, '') AS err "
        f"FROM tasks WHERE {sess_col} = ? AND ({' OR '.join(conds)}) ORDER BY created_at ASC, id ASC",
        params,
    ).fetchall()
out = []
for r in rows:
    item = dict(r)
    try:
        run_row = conn.execute(
            "SELECT COALESCE(summary, '') AS summary, COALESCE(error, '') AS error "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        if run_row:
            if not item["result"] and run_row["summary"]:
                item["result"] = run_row["summary"]
            if not item["err"] and run_row["error"]:
                item["err"] = run_row["error"]
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
	ID           string `json:"id"`
	Title        string `json:"title"`
	Status       string `json:"status"`
	SessionID    string `json:"session_id"`
	Result       string `json:"result"`
	Err          string `json:"err"`
	BlockPayload string `json:"block_payload"`
}

type consoleSessionMeta struct {
	ContextID     string
	CorrelationID string
	Authority     json.RawMessage
}

func (b *Bridge) rememberConsoleSession(sessionID string, origin *lib.Envelope) {
	if sessionID == "" || origin == nil {
		return
	}
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
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
	}
}

func (b *Bridge) markKanbanCardDelivered(cardID string) {
	if cardID == "" {
		return
	}
	b.kanbanMu.Lock()
	defer b.kanbanMu.Unlock()
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
	if b.kanbanDeliveredIDs[cardID] {
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

	b.beginKanbanWait(sessionID)
	defer b.endKanbanWait(sessionID)

	act.inTurn.Store(false)
	act.waitingCard.Store(true)
	defer func() {
		act.waitingCard.Store(false)
		act.inTurn.Store(true)
	}()

	var trackedIDs []string
	trackedSet := make(map[string]bool)
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
				allSettled := true
				for _, c := range relevant {
					if !isKanbanStatusSettled(c.Status) {
						allSettled = false
						break
					}
				}
				if allSettled {
					var parts []string
					for _, c := range relevant {
						b.markKanbanCardDelivered(c.ID)
						if out := formatKanbanCardOutput(c); out != "" {
							parts = append(parts, out)
						}
					}
					if len(parts) > 0 {
						ackText = b.synthesizeCardAnswer(run, taskCtx, sessionID, turn, ackText, relevant, parts)
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
//
// Before: the raw card results were concatenated and *replaced* the model's answer, so a
// 16-cluster fan-out delivered a 27 KB wall of per-card reports with no synthesis (the model's own
// answer at that point is usually a one-liner such as "checking all 16 clusters."). Now the raw
// results go onto the stream as a turn artifact (visible in debug/transcripts) and, unless the
// model already wrote a substantive answer, one more API turn on the same session asks it to
// synthesize them into the final answer. Any failure of that extra turn falls back to the
// concatenation, never to a failed task.
func (b *Bridge) synthesizeCardAnswer(run *taskRun, taskCtx context.Context, sessionID string, turn int, ackText string, cards []kanbanCardRow, parts []string) string {
	joined := strings.Join(parts, "\n\n")
	// The raw card results are the evidence; keep them on the stream regardless of what the
	// answer becomes.
	b.publishTurnAnswer(run, turn*cardTurnArtifactStride, cardResultsHeader+joined)

	if len(strings.TrimSpace(ackText)) >= cardAnswerSubstantiveChars && !isSilentAnswer(ackText) {
		// The model waited for its cards and already synthesized; its answer stands.
		return ackText
	}
	if len(cards) == 1 {
		// One card's report is already a complete answer; a synthesis turn would only restate it
		// (and the chat doctrine tells the model to answer [SILENT] for a single completion).
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
		fmt.Fprintf(&sb, "### Card %d: %s (%s)\n%s\n\n", i+1, title, strings.TrimSpace(c.Status), strings.TrimSpace(parts[minInt(i, len(parts)-1)]))
	}
	synth, ok := b.apiSynthesisTurn(run, taskCtx, sessionID, sb.String(), turn)
	if !ok || isSilentAnswer(synth) {
		return joined
	}
	return synth
}

// isSilentAnswer reports whether text is the chat agents' "nothing to say" sentinel (or empty).
// On a console conversation nobody else posts for the agent, so a silent answer must be replaced
// by the evidence the turn produced rather than delivered as the literal token.
func isSilentAnswer(text string) bool {
	t := strings.TrimSpace(text)
	return t == "" || strings.EqualFold(t, silentSentinel) || strings.EqualFold(strings.Trim(t, "`*_ "), silentSentinel)
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// apiSynthesisTurn posts one extra, non-steer turn to the session and returns its answer. It is
// deliberately soft: no finalize on any failure (the caller falls back), because the task already
// has a deliverable and this turn only improves it.
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
