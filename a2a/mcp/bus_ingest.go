package mcp

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const (
	maxSeenEnvelopes = 16384
	maxRetainedTasks = 1024
)

func (s *BusSession) pruneSessionMapsLocked() {
	if len(s.seenEnvelopes) > maxSeenEnvelopes {
		s.seenEnvelopes = make(map[string]bool, maxSeenEnvelopes/2)
	}
	now := time.Now()
	for id, exp := range s.foregroundTasks {
		if now.After(exp) {
			delete(s.foregroundTasks, id)
		}
	}
	if len(s.tasks) > maxRetainedTasks {
		type taskAge struct {
			id string
			ts time.Time
		}
		var candidates []taskAge
		for id, rec := range s.tasks {
			if rec != nil && rec.Terminal && s.watchedTasks[id] == 0 {
				candidates = append(candidates, taskAge{id: id, ts: rec.UpdatedAt})
			}
		}
		sort.Slice(candidates, func(i, j int) bool {
			return candidates[i].ts.Before(candidates[j].ts)
		})
		toDrop := len(s.tasks) - (maxRetainedTasks * 3 / 4)
		for i := 0; i < len(candidates) && i < toDrop; i++ {
			id := candidates[i].id
			delete(s.tasks, id)
			delete(s.backfilledTasks, id)
			delete(s.prevReportedSteps, id)
			delete(s.lastReportedSteps, id)
			delete(s.prevReportedActs, id)
			delete(s.lastReportedActs, id)
			delete(s.lastWaitStepTitle, id)
		}
	}
}

func (s *BusSession) ingestA2AMessage(subject string, data []byte, isLive bool) {
	switch {
	case strings.HasPrefix(subject, subjectTasksPrefix):
		s.ingestTaskEnvelope(subject, data, isLive)
	case strings.HasPrefix(subject, subjectAgentsPrefix) && strings.HasSuffix(subject, subjectAgentsCardSuffix):
		s.ingestAgentCard(subject, data)
	case strings.HasPrefix(subject, subjectTopicsPrefix):
		s.ingestTopicEnvelope(subject, data)
	}
}

func (s *BusSession) isContextOrParentCanceledLocked(rec *TaskRecord) bool {
	if rec == nil {
		return false
	}
	if rec.State == lib.StateCanceled {
		return true
	}
	if strings.HasPrefix(rec.TaskID, cardTaskIDPrefix) {
		if parentID := firstNonEmptyStr(rec.CardParentTaskID, rec.DelegatedFromTaskID); parentID != "" {
			if p, ok := s.tasks[parentID]; ok && (p.State == lib.StateCanceled || p.State == lib.StateCompleted) {
				return true
			}
		}
		if rec.ContextID != "" {
			var latest *TaskRecord
			for _, other := range s.tasks {
				if other != rec && other.ContextID == rec.ContextID && !strings.HasPrefix(other.TaskID, cardTaskIDPrefix) {
					if latest == nil || other.CreatedAt.After(latest.CreatedAt) {
						latest = other
					}
				}
			}
			if latest != nil && (latest.State == lib.StateCanceled || latest.State == lib.StateCompleted) {
				return true
			}
		}
	}
	return false
}

func (s *BusSession) ingestTaskEnvelope(subject string, data []byte, isLive bool) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil || env.TaskID == "" {
		return
	}

	s.mu.Lock()
	if env.EnvelopeID != "" && s.seenEnvelopes[env.EnvelopeID] {
		s.mu.Unlock()
		return
	}
	if env.EnvelopeID != "" {
		if len(s.seenEnvelopes) >= maxSeenEnvelopes {
			s.pruneSessionMapsLocked()
		}
		s.seenEnvelopes[env.EnvelopeID] = true
	}

	addressee := extractTaskAddressee(subject)
	rec, exists := s.tasks[env.TaskID]
	if !exists {
		if len(s.tasks) >= maxRetainedTasks {
			s.pruneSessionMapsLocked()
		}
		rec = &TaskRecord{
			TaskID:        env.TaskID,
			ContextID:     env.ContextID,
			CorrelationID: env.CorrelationID,
			Addressee:     addressee,
			State:         lib.StateSubmitted,
			CreatedAt:     env.TS,
			UpdatedAt:     env.TS,
		}
		s.tasks[env.TaskID] = rec
	}
	if rec.ContextID == "" {
		rec.ContextID = env.ContextID
	}
	if rec.CorrelationID == "" {
		rec.CorrelationID = env.CorrelationID
	}
	if rec.Addressee == "" && addressee != "" {
		rec.Addressee = addressee
	}
	if env.TS.After(rec.UpdatedAt) {
		rec.UpdatedAt = env.TS
	}
	// Live delivery can race the stream bootstrap: a task first seen through a live envelope gets
	// its true start once the earlier history arrives, so elapsed time is measured from the
	// submission rather than from the moment this process (re)connected.
	if !env.TS.IsZero() && (rec.CreatedAt.IsZero() || env.TS.Before(rec.CreatedAt)) {
		rec.CreatedAt = env.TS
	}
	rec.EnvelopeCount++
	if rec.EnvelopeCountsByKind == nil {
		rec.EnvelopeCountsByKind = make(map[string]int)
	}
	rec.EnvelopeCountsByKind[string(env.Kind)]++

	if len(env.Authority) > 0 {
		var auth authorityWire
		if json.Unmarshal(env.Authority, &auth) == nil {
			if auth.Audience.Conversation != "" && rec.Conversation == "" {
				rec.Conversation = auth.Audience.Conversation
			}
			parentTaskID := ""
			if auth.Via != nil && auth.Via.TaskID != "" {
				parentTaskID = auth.Via.TaskID
			} else if auth.DelegatedFrom != nil && auth.DelegatedFrom.TaskID != "" {
				parentTaskID = auth.DelegatedFrom.TaskID
			}
			if parentTaskID != "" && rec.DelegatedFromTaskID == "" {
				rec.DelegatedFromTaskID = parentTaskID
				if parent, ok := s.tasks[rec.DelegatedFromTaskID]; ok {
					for i := range parent.Delegates {
						if parent.Delegates[i].ChildTaskID == "" || parent.Delegates[i].ChildTaskID == rec.TaskID {
							parent.Delegates[i].ChildTaskID = rec.TaskID
							break
						}
					}
				}
			}
		}
	}
	if rec.Conversation == "" && rec.ContextID != "" {
		for _, other := range s.tasks {
			if other != rec && other.ContextID == rec.ContextID && other.Conversation != "" {
				rec.Conversation = other.Conversation
				break
			}
		}
	}
	if rec.CardParentTaskID == "" && strings.HasPrefix(rec.TaskID, cardTaskIDPrefix) {
		rec.CardParentTaskID = s.findCardParentLocked(rec)
	}

	var pushToFire *PushEvent
	watched := s.isWatchedLocked(rec.Conversation, rec.TaskID, firstNonEmptyStr(rec.DelegatedFromTaskID, rec.CardParentTaskID))

	switch env.Kind {
	case lib.KindMessage:
		var msg lib.Message
		if json.Unmarshal(env.Payload, &msg) == nil {
			text := joinMessageText(msg.Parts)
			if rec.PromptText == "" {
				rec.PromptText = text
				rec.promptTS = env.TS
				if childID := extractWakeChildTaskID(text); childID != "" {
					rec.WakeFromChildTaskID = childID
				}
			} else if !env.TS.IsZero() && !rec.promptTS.IsZero() && env.TS.Before(rec.promptTS) {
				if rec.PromptText != "" {
					rec.SteerTurns = append([]string{rec.PromptText}, rec.SteerTurns...)
					rec.SteerTimestamps = append([]time.Time{rec.promptTS}, rec.SteerTimestamps...)
				}
				rec.PromptText = text
				rec.promptTS = env.TS
				if childID := extractWakeChildTaskID(text); childID != "" {
					rec.WakeFromChildTaskID = childID
				}
			} else if text != "" {
				rec.SteerTurns = append(rec.SteerTurns, text)
				rec.SteerTimestamps = append(rec.SteerTimestamps, env.TS)
			}
		}
	case lib.KindStatusUpdate:
		var st lib.StatusUpdate
		if json.Unmarshal(env.Payload, &st) == nil {
			prevTerminal := rec.Terminal
			prevState := rec.State
			newTerminal := st.Final || st.Status.State.Terminal()
			isOlder := !rec.lastStatusTS.IsZero() && !env.TS.IsZero() && env.TS.Before(rec.lastStatusTS)
			if (!prevTerminal || newTerminal) && (!isOlder || newTerminal) &&
				!(st.Status.State == lib.StateSubmitted && (prevState == lib.StateWorking || prevState == lib.StateInputRequired || prevTerminal)) {
				rec.State = st.Status.State
				rec.Terminal = prevTerminal || newTerminal
				if !env.TS.IsZero() && (rec.lastStatusTS.IsZero() || env.TS.After(rec.lastStatusTS)) {
					rec.lastStatusTS = env.TS
				}
				if st.Status.Message != nil {
					if msgTxt := joinMessageText(st.Status.Message.Parts); msgTxt != "" || !isOlder {
						rec.StatusText = msgTxt
					}
				}
			}
			if isLive && s.bootstrapped && !watched {
				if rec.Terminal && !prevTerminal && !s.isContextOrParentCanceledLocked(rec) {
					kind := eventKindTaskCompleted
					title := fmt.Sprintf("Task %s (%s) %s", rec.TaskID, rec.Addressee, rec.State)
					if rec.State == lib.StateFailed || rec.State == lib.StateRejected || rec.State == lib.StateCanceled {
						kind = eventKindTaskFailed
					}
					summary := rec.ResultText
					if summary == "" {
						summary = rec.StatusText
					}
					if summary == "" {
						summary = rec.ProgressText
					}
					pushToFire = &PushEvent{
						ID:           fmt.Sprintf("task-term-%s-%s", rec.TaskID, rec.State),
						Kind:         kind,
						Deployment:   s.profile.Name,
						Conversation: rec.Conversation,
						TaskID:       rec.TaskID,
						ContextID:    rec.ContextID,
						Subject:      subject,
						Title:        title,
						Summary:      truncateText(summary, pushSummaryMaxChars),
						PRLinks:      append([]string(nil), rec.PRLinks...),
						Timestamp:    env.TS,
					}
				} else if st.Status.State == lib.StateInputRequired && prevState != lib.StateInputRequired {
					pushToFire = &PushEvent{
						ID:           fmt.Sprintf("task-input-%s-%d", rec.TaskID, rec.EnvelopeCount),
						Kind:         eventKindInputRequired,
						Deployment:   s.profile.Name,
						Conversation: rec.Conversation,
						TaskID:       rec.TaskID,
						ContextID:    rec.ContextID,
						Subject:      subject,
						Title:        fmt.Sprintf("Input Required on Task %s (%s)", rec.TaskID, rec.Addressee),
						Summary:      firstNonEmptyStr(rec.StatusText, rec.ProgressText, rec.ResultText),
						Timestamp:    env.TS,
					}
				}
			}
		}
	case lib.KindArtifactUpdate:
		var au lib.ArtifactUpdate
		if json.Unmarshal(env.Payload, &au) == nil {
			art := au.Artifact
			if art.Name != "" {
				rec.EnvelopeCountsByKind["artifact:"+art.Name]++
			}
			text := joinMessageText(art.Parts)
			dataText := joinDataOrTextParts(art.Parts)
			chatMsg := extractChatMessage(art.Parts)
			switch art.Name {
			case lib.ArtifactThinking:
				if text != "" || !au.Append {
					rec.thinkingChunks = append(rec.thinkingChunks, timestampedChunk{
						ts:     env.TS,
						seq:    rec.EnvelopeCount,
						append: au.Append,
						text:   text,
					})
					rec.Thinking = foldTimestampedChunks(rec.thinkingChunks, "\n")
				}
			case lib.ArtifactActivity:
				for _, p := range art.Parts {
					raw := ""
					if p.Kind == partKindData && len(p.Data) > 0 {
						raw = string(p.Data)
					} else if p.Kind == partKindText && p.Text != "" {
						raw = p.Text
					}
					if item, ok := parseActivityItem(raw); ok {
						if item.At == "" && !env.TS.IsZero() {
							item.At = env.TS.UTC().Format(time.RFC3339Nano)
						}
						rec.Activities = upsertActivityItem(rec.Activities, item)
					}
				}
			case lib.ArtifactProgress:
				if text != "" || !au.Append {
					rec.progressChunks = append(rec.progressChunks, timestampedChunk{
						ts:     env.TS,
						seq:    rec.EnvelopeCount,
						append: au.Append,
						text:   text,
					})
					rec.ProgressText = foldTimestampedChunks(rec.progressChunks, "\n")
				}
				if chatMsg != nil && (rec.lastProgressTS.IsZero() || !env.TS.Before(rec.lastProgressTS)) {
					rec.ProgressChat = chatMsg
					rec.lastProgressTS = env.TS
				}
			case lib.ArtifactDelegate:
				var del DelegateItem
				if json.Unmarshal([]byte(dataText), &del) == nil && del.Addressee != "" {
					if del.ChildTaskID == "" {
						for _, other := range s.tasks {
							if other.DelegatedFromTaskID == rec.TaskID {
								alreadyLinked := false
								for _, existing := range rec.Delegates {
									if existing.ChildTaskID == other.TaskID {
										alreadyLinked = true
										break
									}
								}
								if !alreadyLinked {
									del.ChildTaskID = other.TaskID
									break
								}
							}
						}
					}
					rec.Delegates = append(rec.Delegates, del)
				}
			case lib.ArtifactNotice:
				if text != "" {
					rec.Notices = append(rec.Notices, text)
					prs := ExtractPRReferences(text)
					for _, pr := range prs {
						if !containsString(rec.PRLinks, pr) {
							rec.PRLinks = append(rec.PRLinks, pr)
						}
					}
					if isLive && s.bootstrapped && !watched && len(prs) > 0 {
						pushToFire = &PushEvent{
							ID:           fmt.Sprintf("notice-pr-%s-%d", rec.TaskID, len(rec.Notices)),
							Kind:         eventKindNoticePR,
							Deployment:   s.profile.Name,
							Conversation: rec.Conversation,
							TaskID:       rec.TaskID,
							ContextID:    rec.ContextID,
							Subject:      subject,
							Title:        fmt.Sprintf("Pull Request Notice on Task %s", rec.TaskID),
							Summary:      text,
							PRLinks:      prs,
							Timestamp:    env.TS,
						}
					}
				}
			case lib.ArtifactTurn:
				if text != "" {
					rec.IntermediateTurns = append(rec.IntermediateTurns, text)
				}
			case lib.ArtifactResult:
				if text != "" || !au.Append {
					rec.resultChunks = append(rec.resultChunks, timestampedChunk{
						ts:     env.TS,
						seq:    rec.EnvelopeCount,
						append: au.Append,
						text:   text,
					})
					rec.ResultText = foldTimestampedChunks(rec.resultChunks, "")
				}
				if chatMsg != nil && (rec.lastResultTS.IsZero() || !env.TS.Before(rec.lastResultTS)) {
					rec.ResultChat = chatMsg
					rec.lastResultTS = env.TS
					for _, lnk := range chatMsg.Links {
						for _, pr := range ExtractPRReferences(lnk.URL) {
							if !containsString(rec.PRLinks, pr) {
								rec.PRLinks = append(rec.PRLinks, pr)
							}
						}
					}
				}
				for _, pr := range ExtractPRReferences(rec.ResultText) {
					if !containsString(rec.PRLinks, pr) {
						rec.PRLinks = append(rec.PRLinks, pr)
					}
				}
			}
		}
	}
	s.mu.Unlock()

	if pushToFire != nil && s.onPushEvent != nil {
		s.onPushEvent(*pushToFire)
	}
}

func (s *BusSession) isWatchedLocked(conv, taskID, parentTaskID string) bool {
	if conv != "" && s.watchedConvs[conv] > 0 {
		return true
	}
	if taskID != "" && s.watchedTasks[taskID] > 0 {
		return true
	}
	if parentTaskID != "" && s.watchedTasks[parentTaskID] > 0 {
		return true
	}
	// A card's parent may itself be a delegated child of the foreground task: walk one more hop.
	if parentTaskID != "" {
		if parent, ok := s.tasks[parentTaskID]; ok {
			if gp := firstNonEmptyStr(parent.DelegatedFromTaskID, parent.CardParentTaskID); gp != "" && s.watchedTasks[gp] > 0 {
				return true
			}
			if gp := firstNonEmptyStr(parent.DelegatedFromTaskID, parent.CardParentTaskID); gp != "" && s.foregroundTasks != nil {
				if exp, ok := s.foregroundTasks[gp]; ok && time.Now().Before(exp) {
					return true
				}
			}
		}
	}
	now := time.Now()
	if taskID != "" && s.foregroundTasks != nil {
		if exp, ok := s.foregroundTasks[taskID]; ok && now.Before(exp) {
			return true
		}
	}
	if parentTaskID != "" && s.foregroundTasks != nil {
		if exp, ok := s.foregroundTasks[parentTaskID]; ok && now.Before(exp) {
			return true
		}
	}
	return false
}

func (s *BusSession) markForegroundStepTask(taskID string, settled bool) {
	if taskID == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.foregroundTasks == nil {
		s.foregroundTasks = make(map[string]time.Time)
	}
	if settled {
		s.foregroundTasks[taskID] = time.Now().Add(24 * time.Hour)
	} else {
		s.foregroundTasks[taskID] = time.Now().Add(90 * time.Second)
	}
}

func (s *BusSession) ingestAgentCard(subject string, data []byte) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return
	}
	var card map[string]any
	if err := json.Unmarshal(env.Payload, &card); err != nil {
		return
	}
	name := strings.TrimPrefix(subject, subjectAgentsPrefix)
	name = strings.TrimSuffix(name, subjectAgentsCardSuffix)
	s.mu.Lock()
	s.agentCards[name] = card
	s.mu.Unlock()
}

func (s *BusSession) ingestTopicEnvelope(subject string, data []byte) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return
	}
	var art lib.Artifact
	if err := json.Unmarshal(env.Payload, &art); err != nil {
		return
	}
	var summary string
	var rawData json.RawMessage
	for _, p := range art.Parts {
		switch p.Kind {
		case partKindText:
			summary = strings.TrimSpace(p.Text)
		case partKindData:
			rawData = p.Data
		}
	}
	stream := streamTopicsState
	class := topicClassState
	if strings.HasPrefix(subject, subjectTopicsJournalPrefix) {
		stream = streamTopicsJournal
		class = topicClassJournal
	}
	topicName := art.Name
	if topicName == "" {
		parts := strings.Split(subject, ".")
		if len(parts) > 0 {
			topicName = parts[len(parts)-1]
		}
	}
	snap := TopicSnapshot{
		Stream:        stream,
		Subject:       subject,
		Topic:         topicName,
		Class:         class,
		WriterSession: env.From.Session,
		WriterProfile: env.From.Profile,
		TaskID:        env.TaskID,
		CorrelationID: env.CorrelationID,
		Summary:       summary,
		Data:          rawData,
		UpdatedAt:     env.TS,
	}
	s.mu.Lock()
	s.topics[subject] = snap
	s.mu.Unlock()
}

func (s *BusSession) ingestConsoleOut(subject string, data []byte) {
	var frame ConsoleOutFrame
	if err := json.Unmarshal(data, &frame); err != nil {
		return
	}
	token := strings.TrimPrefix(subject, subjectConsoleOutPrefix)
	token = strings.TrimSuffix(token, subjectConsoleOutSuffix)
	frame.Token = token
	frame.Timestamp = time.Now().UTC()

	isEdit := false
	switch v := frame.Edit.(type) {
	case bool:
		isEdit = v
	case string:
		isEdit = strings.TrimSpace(v) != ""
	}

	s.mu.Lock()
	if isEdit && frame.MessageID != "" {
		updated := false
		for i := len(s.consoleNotices) - 1; i >= 0; i-- {
			if s.consoleNotices[i].Token == token && s.consoleNotices[i].MessageID == frame.MessageID {
				s.consoleNotices[i].Text = frame.Text
				s.consoleNotices[i].Timestamp = frame.Timestamp
				updated = true
				break
			}
		}
		if !updated {
			s.consoleNotices = append(s.consoleNotices, frame)
		}
	} else {
		s.consoleNotices = append(s.consoleNotices, frame)
	}
	if len(s.consoleNotices) > maxConsoleNoticesRetained {
		s.consoleNotices = s.consoleNotices[len(s.consoleNotices)-maxConsoleNoticesRetained:]
	}
	s.mu.Unlock()
}

func (s *BusSession) handlePassiveNotify(msg *nats.Msg) {
	var req lib.NotifyRequest
	if err := json.Unmarshal(msg.Data, &req); err != nil {
		return
	}
	title := defaultNotifyTitle
	summary := strings.TrimSpace(req.Text)
	var parsedChat *chatmsg.Message
	if len(req.Chat) > 0 {
		if cm, cErr := chatmsg.Parse(req.Chat); cErr == nil {
			parsedChat = &cm
		}
	}
	if parsedChat != nil {
		if parsedChat.Headline != "" {
			title = parsedChat.Headline
		}
		if summary == "" {
			summary = parsedChat.Detail
		}
	}
	if summary == "" {
		summary = title
	}
	prs := ExtractPRReferences(summary)
	if parsedChat != nil {
		for _, lnk := range parsedChat.Links {
			for _, pr := range ExtractPRReferences(lnk.URL) {
				if !containsString(prs, pr) {
					prs = append(prs, pr)
				}
			}
		}
	}
	if s.onPushEvent != nil {
		s.onPushEvent(PushEvent{
			ID:           fmt.Sprintf("notify-%d", time.Now().UnixNano()),
			Kind:         eventKindProactiveNotify,
			Deployment:   s.profile.Name,
			Conversation: req.Conversation,
			ContextID:    req.ContextID,
			Subject:      msg.Subject,
			Title:        title,
			Summary:      summary,
			PRLinks:      prs,
			Raw:          append(json.RawMessage(nil), msg.Data...),
			Timestamp:    time.Now().UTC(),
		})
	}
}

func extractTaskAddressee(subject string) string {
	trimmed := strings.TrimPrefix(subject, subjectTasksPrefix)
	parts := strings.Split(trimmed, ".")
	if len(parts) >= 2 {
		return parts[0]
	}
	return ""
}

func extractWakeChildTaskID(prompt string) string {
	if strings.HasPrefix(prompt, wakePromptPrefix) {
		rest := strings.TrimPrefix(prompt, wakePromptPrefix)
		fields := strings.Fields(rest)
		if len(fields) > 0 {
			return strings.TrimRight(fields[0], "]")
		}
		return ""
	}
	for _, marker := range []string{"The task you delegated to ", "You delegated to "} {
		if mIdx := strings.Index(prompt, marker); mIdx >= 0 {
			afterMarker := prompt[mIdx+len(marker):]
			if idx := strings.Index(afterMarker, "(task "); idx >= 0 && idx < 80 {
				rest := afterMarker[idx+len("(task "):]
				if end := strings.IndexByte(rest, ')'); end > 0 {
					return strings.TrimSpace(rest[:end])
				}
			}
		}
	}
	return ""
}

func joinMessageText(parts []lib.Part) string {
	var chunks []string
	for _, p := range parts {
		if p.Kind == partKindText && p.Text != "" {
			chunks = append(chunks, p.Text)
		}
	}
	return strings.Join(chunks, "\n")
}

func joinDataOrTextParts(parts []lib.Part) string {
	for _, p := range parts {
		if p.Kind == partKindData && len(p.Data) > 0 {
			return string(p.Data)
		}
		if p.Kind == partKindText && p.Text != "" {
			return p.Text
		}
	}
	return ""
}

func extractChatMessage(parts []lib.Part) *chatmsg.Message {
	for _, p := range parts {
		if p.Kind == partKindData && len(p.Data) > 0 {
			if msg, err := chatmsg.Parse(p.Data); err == nil && msg.Schema != "" {
				return &msg
			}
		}
	}
	return nil
}

func parseActivityItem(raw string) (ActivityItem, bool) {
	var wire struct {
		Tool            string          `json:"tool"`
		Input           json.RawMessage `json:"input"`
		CallID          string          `json:"callId"`
		CallIDSnake     string          `json:"call_id"`
		Status          string          `json:"status"`
		ErrorType       string          `json:"errorType"`
		ErrTypeSnake    string          `json:"error_type"`
		ErrorMessage    string          `json:"errorMessage"`
		ErrMsgSnake     string          `json:"error_message"`
		ResultPreview   string          `json:"resultPreview"`
		ResPreviewSnake string          `json:"result_preview"`
		SessionID       string          `json:"sessionId"`
		SessionIDSnake  string          `json:"session_id"`
		Worker          string          `json:"worker"`
		DurationMs      int64           `json:"durationMs"`
		DurMsSnake      int64           `json:"duration_ms"`
		At              string          `json:"at"`
	}
	if err := json.Unmarshal([]byte(raw), &wire); err != nil || wire.Tool == "" {
		return ActivityItem{}, false
	}
	inputStr := ""
	if len(wire.Input) > 0 {
		var s string
		if json.Unmarshal(wire.Input, &s) == nil {
			inputStr = s
		} else {
			inputStr = strings.TrimSpace(string(wire.Input))
			if inputStr == jsonNullLiteral {
				inputStr = ""
			}
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeLt, literalLt)
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeGt, literalGt)
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeAmp, literalAmp)
		}
	}
	dur := wire.DurationMs
	if dur == 0 {
		dur = wire.DurMsSnake
	}
	return ActivityItem{
		Tool:          wire.Tool,
		Input:         inputStr,
		CallID:        firstNonEmptyStr(wire.CallID, wire.CallIDSnake),
		Status:        wire.Status,
		ErrorType:     firstNonEmptyStr(wire.ErrorType, wire.ErrTypeSnake),
		ErrorMessage:  firstNonEmptyStr(wire.ErrorMessage, wire.ErrMsgSnake),
		ResultPreview: firstNonEmptyStr(wire.ResultPreview, wire.ResPreviewSnake),
		SessionID:     firstNonEmptyStr(wire.SessionID, wire.SessionIDSnake),
		Worker:        wire.Worker,
		DurationMs:    dur,
		At:            wire.At,
	}, true
}

func parseActivityTimestamp(at string) (time.Time, bool) {
	at = strings.TrimSpace(at)
	if at == "" {
		return time.Time{}, false
	}
	if t, err := time.Parse(time.RFC3339Nano, at); err == nil {
		return t, true
	}
	if t, err := time.Parse(time.RFC3339, at); err == nil {
		return t, true
	}
	return time.Time{}, false
}

func upsertActivityItem(list []ActivityItem, item ActivityItem) []ActivityItem {
	replaced := false
	if item.CallID != "" {
		for i := range list {
			if list[i].CallID == item.CallID {
				item.Index = i + 1
				list[i] = item
				replaced = true
				break
			}
		}
	}
	if !replaced {
		item.Index = len(list) + 1
		list = append(list, item)
	}
	if len(list) > 1 {
		needSort := false
		for i := 1; i < len(list); i++ {
			ti, okI := parseActivityTimestamp(list[i].At)
			tj, okJ := parseActivityTimestamp(list[i-1].At)
			if okI && okJ && ti.Before(tj) {
				needSort = true
				break
			}
		}
		if needSort {
			sort.SliceStable(list, func(i, j int) bool {
				ti, okI := parseActivityTimestamp(list[i].At)
				tj, okJ := parseActivityTimestamp(list[j].At)
				if okI && okJ && !ti.Equal(tj) {
					return ti.Before(tj)
				}
				return false
			})
		}
	}
	backfillActivityWorkers(list)
	return list
}

func backfillActivityWorkers(list []ActivityItem) {
	cardTitles := make(map[string]string)
	cardAssignees := make(map[string]string)
	cardClusters := make(map[string]string)
	sessToCard := make(map[string]string)

	for i := range list {
		list[i].Index = i + 1
		act := list[i]
		var m map[string]any
		if act.Input != "" && strings.HasPrefix(strings.TrimSpace(act.Input), "{") {
			_ = json.Unmarshal([]byte(act.Input), &m)
		}
		if act.Tool == "kanban_create" {
			cid := extractCardIDFromPreview(act.ResultPreview)
			if cid == "" {
				cid = firstNonEmptyArgString(m, "task_id", "card_id", "id")
			}
			rawTitle, _ := m["title"].(string)
			title := cleanShortCardTitle(rawTitle)
			assignee, _ := m["assignee"].(string)
			assignee = strings.ToLower(strings.TrimSpace(assignee))
			cluster := strings.ToLower(extractClusterFromAssignee(assignee))
			if cid != "" {
				if title != "" {
					cardTitles[cid] = title
					cardClusters[strings.ToLower(title)] = cid
				}
				if assignee != "" {
					cardAssignees[assignee] = cid
				}
				if cluster != "" {
					cardClusters[cluster] = cid
				}
			}
		} else if strings.HasPrefix(act.Tool, "kanban_") {
			cid := firstNonEmptyArgString(m, "card_id", "task_id", "id")
			if cid != "" && act.SessionID != "" && !strings.HasPrefix(act.SessionID, "a2a-") {
				sessToCard[act.SessionID] = cid
			}
		}
		if strings.HasPrefix(act.Worker, "card:") && act.SessionID != "" && !strings.HasPrefix(act.SessionID, "a2a-") {
			rest := strings.TrimPrefix(act.Worker, "card:")
			if idx := strings.IndexByte(rest, ' '); idx > 0 {
				rest = rest[:idx]
			}
			if rest != "" {
				sessToCard[act.SessionID] = rest
			}
		}
	}

	for i := range list {
		act := list[i]
		if act.SessionID == "" || strings.HasPrefix(act.SessionID, "a2a-") || sessToCard[act.SessionID] != "" {
			continue
		}
		lowerIn := strings.ToLower(act.Input)
		if lowerIn == "" {
			continue
		}
		for assignee, cid := range cardAssignees {
			if assignee != "" && cid != "" && strings.Contains(lowerIn, assignee) {
				sessToCard[act.SessionID] = cid
				break
			}
		}
		if sessToCard[act.SessionID] == "" {
			for cluster, cid := range cardClusters {
				if cluster != "" && cid != "" && strings.Contains(lowerIn, cluster) {
					sessToCard[act.SessionID] = cid
					break
				}
			}
		}
		if sessToCard[act.SessionID] == "" {
			for cid := range cardTitles {
				if cid != "" && strings.Contains(lowerIn, strings.ToLower(cid)) {
					sessToCard[act.SessionID] = cid
					break
				}
			}
		}
	}

	if len(cardTitles) == 1 {
		var soleCID string
		for cid := range cardTitles {
			soleCID = cid
		}
		for i := range list {
			act := list[i]
			if act.SessionID != "" && !strings.HasPrefix(act.SessionID, "a2a-") && sessToCard[act.SessionID] == "" {
				sessToCard[act.SessionID] = soleCID
			}
		}
	}

	for i := range list {
		act := &list[i]
		if act.SessionID != "" && !strings.HasPrefix(act.SessionID, "a2a-") {
			if cid := sessToCard[act.SessionID]; cid != "" {
				if t := cardTitles[cid]; t != "" {
					act.Worker = fmt.Sprintf("card:%s (%s)", cid, t)
				} else {
					act.Worker = "card:" + cid
				}
			} else if act.Worker == "" {
				act.Worker = "worker:" + truncateText(act.SessionID, 20)
			}
		} else if act.Worker == "" && strings.HasPrefix(act.SessionID, "a2a-") {
			act.Worker = "platform"
		}
	}
}

func extractCardIDFromPreview(preview string) string {
	for _, field := range strings.Fields(preview) {
		if strings.HasPrefix(field, "card_id=") {
			return strings.Trim(strings.TrimPrefix(field, "card_id="), ",;:.)]\"'")
		}
		if strings.HasPrefix(field, "t_") {
			return strings.Trim(field, ",;:.)]\"'")
		}
	}
	return ""
}

func cleanShortCardTitle(title string) string {
	title = strings.TrimSpace(title)
	if title == "" {
		return ""
	}
	lower := strings.ToLower(title)
	for _, sep := range []string{" for ", ": "} {
		if idx := strings.LastIndex(lower, sep); idx >= 0 && idx+len(sep) < len(title) {
			candidate := strings.TrimSpace(title[idx+len(sep):])
			if candidate != "" && !strings.Contains(candidate, " ") {
				return truncateText(candidate, 48)
			}
		}
	}
	return truncateText(title, 48)
}

func isPureControlCommand(text string) bool {
	lower := strings.ToLower(strings.TrimSpace(text))
	return lower == cmdSessionOn || lower == cmdSessionOff || lower == cmdStatus || lower == cmdStop || lower == cmdHelp
}

func firstNonEmptyStr(vals ...string) string {
	for _, v := range vals {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

func truncateText(s string, maxLen int) string {
	s = strings.TrimSpace(s)
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + ellipsisSuffix
}

func containsString(slice []string, target string) bool {
	for _, s := range slice {
		if s == target {
			return true
		}
	}
	return false
}
