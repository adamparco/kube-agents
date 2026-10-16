package mcp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const (
	badgeActive              = "🟢 **Active**"
	badgeStandby             = "⚪ Standby"
	badgeReady               = "✅ Ready"
	badgeNotReady            = "⚠️ Not Ready"
	badgeCompleted           = "✅ `completed`"
	badgeWorking             = "⏳ `working`"
	badgeSubmitted           = "📨 `submitted`"
	badgeInputRequired       = "❓ `input-required`"
	badgeFailed              = "❌ `failed`"
	badgeCanceled            = "🚫 `canceled`"
	badgeRejected            = "⛔ `rejected`"
	toneGood                 = "good"
	toneWarn                 = "warn"
	toneBad                  = "bad"
	toneInfo                 = "info"
	severityGood             = "good"
	severityWarn             = "warn"
	severityBad              = "bad"
	severityInfo             = "info"
	planStateDone            = "done"
	planStateActive          = "active"
	planStatePending         = "pending"
	planStateSkipped         = "skipped"
	planStateFailed          = "failed"
	planStateBlocked         = "blocked"
	activityInputMaxChars    = 120
	thinkingPreviewChars     = 90
	topicSummaryPreviewChars = 100
	bytesPerKB               = 1024
	bytesPerMB               = 1024 * 1024
	fileSchemePrefix         = "file://"
)

// FormatDeploymentsMarkdown renders discovered/saved DeploymentProfiles as a rich Markdown table.
func FormatDeploymentsMarkdown(profiles []DeploymentProfile, activeName string) string {
	if len(profiles) == 0 {
		return "No `kube-agents` deployments found in `~/.kube/config` or saved configuration.\n"
	}
	var sb strings.Builder
	sb.WriteString("## 🛰️ Kube-Agents Deployments\n\n")
	sb.WriteString("| Status | Name | Ready | Mode | Cluster / Location | Namespace | GitHub Repo | Chat | Profiles | Kube Context |\n")
	sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
	for _, p := range profiles {
		activeCol := badgeStandby
		if p.Name == activeName {
			activeCol = badgeActive
		}
		readyCol := badgeNotReady
		if p.Ready {
			readyCol = badgeReady
		}
		clusterLoc := p.ClusterName
		if p.Location != "" {
			clusterLoc = fmt.Sprintf("%s (`%s`)", firstNonEmptyStr(p.ClusterName, p.PlatformAgentName), p.Location)
		}
		profs := "—"
		if len(p.AgentProfiles) > 0 {
			profs = "`" + strings.Join(p.AgentProfiles, "`, `") + "`"
		}
		fmt.Fprintf(&sb, "| %s | **`%s`** | %s | `%s` | %s | `%s` | `%s` | `%s` | %s | `%s` |\n",
			activeCol,
			p.Name,
			readyCol,
			firstNonEmptyStr(p.Mode, "next"),
			firstNonEmptyStr(clusterLoc, "—"),
			p.Namespace,
			firstNonEmptyStr(p.GitHubRepo, "—"),
			firstNonEmptyStr(p.ChatPlatform, "console"),
			profs,
			firstNonEmptyStr(p.KubeContext, p.DirectNATSURL),
		)
	}
	return sb.String()
}

// FormatBusStatusMarkdown renders the live connection and JetStream bus health dashboard.
func FormatBusStatusMarkdown(rep BusStatusReport, conv ConversationEntry, push PushConfig, activeJetskiID string) string {
	var sb strings.Builder
	sb.WriteString("## 🔌 Kube-Agents A2A JetStream Bus Status\n\n")
	modeLabel := "Platform Agent (`platform` / Hermes)"
	if conv.SessionMode {
		modeLabel = "Persistent Session Worker (`/session` / Claude Code)"
	}
	pushLabel := "Piggyback Drain Only (no Jetski Conversation ID bound)"
	if push.Enabled && activeJetskiID != "" {
		pushLabel = fmt.Sprintf("🟢 Armed (`agentapi` -> `%s` + Piggyback Drain)", activeJetskiID)
	} else if activeJetskiID != "" {
		pushLabel = fmt.Sprintf("🟡 Bound to `%s` (Piggyback Drain active)", activeJetskiID)
	}
	observerLabel := "Inactive"
	if rep.ObserverAttached {
		observerLabel = "🟢 Active (`chat.notify.*` passive tap)"
	}

	sb.WriteString("| Property | Value |\n| :--- | :--- |\n")
	fmt.Fprintf(&sb, "| **Active Deployment** | **`%s`** (`%s/%s`, mode `%s`) |\n",
		rep.Deployment.Name, rep.Deployment.Namespace, rep.Deployment.PlatformAgentName, firstNonEmptyStr(rep.Deployment.Mode, "next"))
	fmt.Fprintf(&sb, "| **Cluster & Project** | `%s` in `%s` (`%s`) |\n",
		firstNonEmptyStr(rep.Deployment.ClusterName, "—"), firstNonEmptyStr(rep.Deployment.ProjectID, "—"), firstNonEmptyStr(rep.Deployment.Location, "—"))
	fmt.Fprintf(&sb, "| **GitHub Repository** | `%s` |\n", firstNonEmptyStr(rep.Deployment.GitHubRepo, "—"))
	fmt.Fprintf(&sb, "| **Bus Endpoint & Identity** | `%s` as `%s` (`_INBOX.%s`) |\n", rep.ConnectedURL, rep.BusUser, rep.BusUser)
	fmt.Fprintf(&sb, "| **Proactive Notify Tap** | %s |\n", observerLabel)
	fmt.Fprintf(&sb, "| **Active Conversation** | **`%s`** (token `console:%s`) — %s |\n", conv.Alias, conv.Token, modeLabel)
	fmt.Fprintf(&sb, "| **Bi-Directional Push** | %s |\n", pushLabel)
	fmt.Fprintf(&sb, "| **Cached State** | `%d` task(s), `%d` topic(s), `%d` agent card(s) |\n\n",
		rep.TaskCount, rep.TopicCount, len(rep.AgentCards))

	if len(rep.Streams) > 0 {
		sb.WriteString("### 📊 JetStream Streams (`a2a-jetstream/0.4`)\n\n")
		sb.WriteString("| Stream | Messages | Size | Capacity | Consumers | Subjects |\n")
		sb.WriteString("| :--- | ---: | ---: | ---: | ---: | :--- |\n")
		for _, st := range rep.Streams {
			fmt.Fprintf(&sb, "| **`%s`** | %d | %s | %s | %d | `%s` |\n",
				st.Name,
				st.Messages,
				formatByteCount(int64(st.Bytes)),
				formatByteCount(st.MaxBytes),
				st.Consumers,
				strings.Join(st.Subjects, "`, `"),
			)
		}
		sb.WriteString("\n")
	}

	if len(rep.KeyConsumers) > 0 {
		sb.WriteString("### 💓 Key Durable Consumers\n\n")
		sb.WriteString("| Stream | Consumer | Pending | Ack Pending | Bound |\n")
		sb.WriteString("| :--- | :--- | ---: | ---: | :--- |\n")
		for _, c := range rep.KeyConsumers {
			fmt.Fprintf(&sb, "| `%s` | **`%s`** | %d | %d | `%v` |\n",
				c.Stream, c.Name, c.NumPending, c.NumAckPending, c.PushBound)
		}
		sb.WriteString("\n")
	}

	if len(rep.AgentCards) > 0 {
		sb.WriteString("### 🤖 Registered Agent Cards (`DIRECTORY`)\n\n")
		names := make([]string, 0, len(rep.AgentCards))
		for k := range rep.AgentCards {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, name := range names {
			card := rep.AgentCards[name]
			desc, _ := card["description"].(string)
			ver, _ := card["version"].(string)
			fmt.Fprintf(&sb, "- **`%s`**", name)
			if ver != "" {
				fmt.Fprintf(&sb, " (`v%s`)", ver)
			}
			if desc != "" {
				fmt.Fprintf(&sb, ": %s", desc)
			}
			sb.WriteString("\n")
		}
	}
	return sb.String()
}

// FormatTurnOutcomeMarkdown renders a complete turn outcome, including console notices,
// primary task execution, thinking, tool calls, delegated child tasks, and wake synthesis.
func FormatTurnOutcomeMarkdown(outcome *TurnOutcome) string {
	if outcome == nil {
		return ""
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "## 💬 Kube-Agents Turn (`%s` / `console:%s`)\n\n", outcome.ConversationAlias, outcome.ConversationToken)

	if len(outcome.ConsoleNotices) > 0 {
		sb.WriteString("### 🔔 Gateway Notices\n")
		for _, n := range outcome.ConsoleNotices {
			fmt.Fprintf(&sb, "- %s\n", n.Text)
		}
		sb.WriteString("\n")
	}

	if len(outcome.ChildTasks) > 0 && outcome.PrimaryTask != nil {
		sb.WriteString("### 🔀 Delegation Flow\n```mermaid\nflowchart LR\n")
		fmt.Fprintf(&sb, "  P[\"%s (%s)\"]\n", outcome.PrimaryTask.Addressee, outcome.PrimaryTask.TaskID)
		for i, child := range outcome.ChildTasks {
			nodeID := fmt.Sprintf("C%d", i)
			fmt.Fprintf(&sb, "  P -->|delegate| %s[\"%s (%s)\"]\n", nodeID, child.Addressee, child.TaskID)
			if outcome.WakeTask != nil {
				fmt.Fprintf(&sb, "  %s -->|wake| W[\"%s (%s)\"]\n", nodeID, outcome.WakeTask.Addressee, outcome.WakeTask.TaskID)
			}
		}
		sb.WriteString("```\n\n")
	}

	if outcome.PrimaryTask != nil {
		sb.WriteString(FormatTaskRecordMarkdown(outcome.PrimaryTask, "Primary Task"))
	}

	for i, child := range outcome.ChildTasks {
		sb.WriteString("\n---\n")
		sb.WriteString(FormatTaskRecordMarkdown(child, fmt.Sprintf("Delegated Child Task #%d", i+1)))
	}

	if outcome.WakeTask != nil {
		sb.WriteString("\n---\n")
		sb.WriteString(FormatTaskRecordMarkdown(outcome.WakeTask, "Session Wake Synthesis Task"))
	}

	allPRs := collectOutcomePRs(outcome)
	if len(allPRs) > 0 {
		sb.WriteString("\n> [!TIP]\n")
		fmt.Fprintf(&sb, "> **Pull Request(s) Detected**: %s\n", strings.Join(allPRs, ", "))
		sb.WriteString("> Call `inspect_pr` to fetch the unified diff and open files natively in Jetski's diff viewer!\n")
	}

	if outcome.TimedOut && outcome.PrimaryTask != nil {
		sb.WriteString("\n> [!NOTE]\n")
		fmt.Fprintf(&sb, "> Task `%s` is still in flight (`%s`). You can call `task_control` (`action: \"get\"` or `\"steer\"` or `\"stop\"`) with `task_id: %q`, or wait for the automatic background push wakeup when it finishes.\n",
			outcome.PrimaryTask.TaskID, outcome.PrimaryTask.State, outcome.PrimaryTask.TaskID)
	}

	return sb.String()
}

// FormatTaskRecordMarkdown renders a single TaskRecord with thinking, activities, progress, and result.
func FormatTaskRecordMarkdown(rec *TaskRecord, titlePrefix string) string {
	if rec == nil {
		return ""
	}
	var sb strings.Builder
	if titlePrefix == "" {
		titlePrefix = "Task"
	}
	fmt.Fprintf(&sb, "### %s: `%s` → `%s` (%s)\n\n",
		titlePrefix, rec.TaskID, rec.Addressee, formatTaskStateBadge(rec.State))

	fmt.Fprintf(&sb, "- **Context ID**: `%s`", rec.ContextID)
	if rec.Conversation != "" {
		fmt.Fprintf(&sb, " | **Conversation**: `%s`", rec.Conversation)
	}
	if rec.DelegatedFromTaskID != "" {
		fmt.Fprintf(&sb, " | **Delegated From**: `%s`", rec.DelegatedFromTaskID)
	}
	if rec.WakeFromChildTaskID != "" {
		fmt.Fprintf(&sb, " | **Wake From Child**: `%s`", rec.WakeFromChildTaskID)
	}
	sb.WriteString("\n\n")

	if strings.TrimSpace(rec.Thinking) != "" {
		thinkingClean := strings.TrimSpace(rec.Thinking)
		preview := truncateText(strings.ReplaceAll(thinkingClean, "\n", " "), thinkingPreviewChars)
		sb.WriteString("<details>\n")
		fmt.Fprintf(&sb, "<summary>💭 <b>Agent Thinking</b> — <i>%s</i></summary>\n\n", preview)
		for _, line := range strings.Split(thinkingClean, "\n") {
			fmt.Fprintf(&sb, "> %s\n", line)
		}
		sb.WriteString("\n</details>\n\n")
	}

	if len(rec.Activities) > 0 {
		sb.WriteString("#### 🛠️ Tool Activity Stream\n\n")
		sb.WriteString("| # | Tool | Status | Duration | Input Summary |\n")
		sb.WriteString("| ---: | :--- | :--- | ---: | :--- |\n")
		for i, act := range rec.Activities {
			statusStr := act.Status
			if statusStr == "" {
				statusStr = "called"
			}
			durStr := "—"
			if act.DurationMs > 0 {
				durStr = fmt.Sprintf("%dms", act.DurationMs)
			}
			inputClean := strings.ReplaceAll(strings.TrimSpace(act.Input), "\n", " ")
			inputClean = strings.ReplaceAll(inputClean, "|", "\\|")
			inputClean = truncateText(inputClean, activityInputMaxChars)
			fmt.Fprintf(&sb, "| %d | `%s` | `%s` | %s | `%s` |\n",
				i+1, act.Tool, statusStr, durStr, firstNonEmptyStr(inputClean, "—"))
		}
		sb.WriteString("\n")
	}

	if len(rec.Delegates) > 0 {
		sb.WriteString("#### 🤝 Delegations Requested\n")
		for _, d := range rec.Delegates {
			childInfo := ""
			if d.ChildTaskID != "" {
				childInfo = fmt.Sprintf(" → child task `%s`", d.ChildTaskID)
			}
			fmt.Fprintf(&sb, "- **To `%s`**%s: %s\n", d.Addressee, childInfo, d.Text)
		}
		sb.WriteString("\n")
	}

	if len(rec.IntermediateTurns) > 0 {
		sb.WriteString("#### 🔄 Intermediate Turns\n")
		for _, t := range rec.IntermediateTurns {
			fmt.Fprintf(&sb, "> %s\n\n", strings.TrimSpace(t))
		}
	}

	if len(rec.Notices) > 0 {
		sb.WriteString("#### 📌 Task Notices\n")
		for _, n := range rec.Notices {
			fmt.Fprintf(&sb, "- %s\n", n)
		}
		sb.WriteString("\n")
	}

	if !rec.Terminal && (rec.ProgressChat != nil || strings.TrimSpace(rec.ProgressText) != "") {
		sb.WriteString("#### 📡 Live Progress\n\n")
		if rec.ProgressChat != nil {
			sb.WriteString(FormatChatMessageMarkdown(rec.ProgressChat))
		} else {
			fmt.Fprintf(&sb, "%s\n\n", strings.TrimSpace(rec.ProgressText))
		}
	}

	if rec.ResultChat != nil || strings.TrimSpace(rec.ResultText) != "" {
		sb.WriteString("#### 🎯 Result\n\n")
		if rec.ResultChat != nil {
			sb.WriteString(FormatChatMessageMarkdown(rec.ResultChat))
			if strings.TrimSpace(rec.ResultText) != "" && rec.ResultChat.Detail == "" && len(rec.ResultChat.Rows) == 0 {
				fmt.Fprintf(&sb, "\n%s\n", strings.TrimSpace(rec.ResultText))
			}
		} else {
			fmt.Fprintf(&sb, "%s\n", strings.TrimSpace(rec.ResultText))
		}
	} else if rec.StatusText != "" {
		fmt.Fprintf(&sb, "**Status Detail**: %s\n", rec.StatusText)
	}

	return sb.String()
}

// FormatChatMessageMarkdown renders a structured kube-agents.chat/v1 Message into rich Jetski Markdown.
func FormatChatMessageMarkdown(m *chatmsg.Message) string {
	if m == nil {
		return ""
	}
	var sb strings.Builder

	if m.Headline != "" {
		alertTag := ""
		switch m.Tone {
		case chatmsg.ToneCritical, toneBad:
			alertTag = "[!CAUTION]"
		case chatmsg.ToneAttention, toneWarn:
			alertTag = "[!WARNING]"
		case chatmsg.ToneSuccess, toneGood:
			alertTag = "[!TIP]"
		case toneInfo:
			alertTag = "[!NOTE]"
		}
		if alertTag != "" {
			fmt.Fprintf(&sb, "> %s\n> **%s**", alertTag, m.Headline)
			if m.Note != "" {
				fmt.Fprintf(&sb, " — *%s*", m.Note)
			}
			sb.WriteString("\n\n")
		} else {
			fmt.Fprintf(&sb, "**%s**", m.Headline)
			if m.Note != "" {
				fmt.Fprintf(&sb, " *(%s)*", m.Note)
			}
			sb.WriteString("\n\n")
		}
	}

	if strings.TrimSpace(m.Detail) != "" {
		fmt.Fprintf(&sb, "%s\n\n", strings.TrimSpace(m.Detail))
	}

	if len(m.Rows) > 0 {
		sb.WriteString("| Status | Item | Detail |\n")
		sb.WriteString("| :--- | :--- | :--- |\n")
		for _, r := range m.Rows {
			fmt.Fprintf(&sb, "| %s | **%s** | %s |\n",
				formatSeverityIcon(r.Severity),
				escapeTableCell(r.Text),
				escapeTableCell(firstNonEmptyStr(r.Detail, "—")),
			)
		}
		sb.WriteString("\n")
	}

	if m.Plan != nil && len(m.Plan.Items) > 0 {
		if m.Plan.Title != "" {
			fmt.Fprintf(&sb, "**📋 Plan: %s**\n", m.Plan.Title)
		} else {
			sb.WriteString("**📋 Execution Plan**\n")
		}
		for _, item := range m.Plan.Items {
			icon := "⬜"
			switch item.Status {
			case chatmsg.StatusComplete, planStateDone:
				icon = "✅"
			case chatmsg.StatusInProgress, planStateActive:
				icon = "🔄"
			case chatmsg.StatusError, planStateFailed:
				icon = "❌"
			case chatmsg.StatusPending:
				icon = "⬜"
			}
			fmt.Fprintf(&sb, "- %s **%s**", icon, firstNonEmptyStr(item.Title, item.ID))
			if extra := firstNonEmptyStr(item.Result, item.Note); extra != "" {
				fmt.Fprintf(&sb, " — %s", extra)
			}
			sb.WriteString("\n")
		}
		sb.WriteString("\n")
	}

	if m.Fold != nil {
		label := firstNonEmptyStr(m.Fold.Title, chatmsg.DefaultFoldTitle)
		sb.WriteString("<details>\n")
		fmt.Fprintf(&sb, "<summary>📂 <b>%s</b></summary>\n\n", label)
		if strings.TrimSpace(m.Fold.Text) != "" {
			fmt.Fprintf(&sb, "%s\n\n", strings.TrimSpace(m.Fold.Text))
		}
		if m.Fold.Table != nil && len(m.Fold.Table.Columns) > 0 {
			sb.WriteString("| " + strings.Join(m.Fold.Table.Columns, " | ") + " |\n")
			sep := make([]string, len(m.Fold.Table.Columns))
			for i := range sep {
				sep[i] = ":---"
			}
			sb.WriteString("| " + strings.Join(sep, " | ") + " |\n")
			for _, row := range m.Fold.Table.Rows {
				cells := make([]string, len(m.Fold.Table.Columns))
				for i := range cells {
					if i < len(row) {
						cells[i] = escapeTableCell(row[i])
					} else {
						cells[i] = "—"
					}
				}
				sb.WriteString("| " + strings.Join(cells, " | ") + " |\n")
			}
			sb.WriteString("\n")
		}
		sb.WriteString("</details>\n\n")
	}

	for _, afterLine := range m.After {
		if strings.TrimSpace(afterLine) != "" {
			fmt.Fprintf(&sb, "%s\n\n", strings.TrimSpace(afterLine))
		}
	}

	if len(m.Choices) > 0 {
		sb.WriteString("**Suggested Actions / Choices:**\n")
		for _, c := range m.Choices {
			reply := firstNonEmptyStr(c.Reply, c.Text)
			fmt.Fprintf(&sb, "- **`%s`** → send `%s`\n", c.Text, reply)
		}
		sb.WriteString("\n")
	}

	if len(m.Links) > 0 {
		sb.WriteString("**Links:** ")
		parts := make([]string, 0, len(m.Links))
		for _, l := range m.Links {
			parts = append(parts, fmt.Sprintf("[%s](%s)", l.Text, l.URL))
		}
		sb.WriteString(strings.Join(parts, " • ") + "\n\n")
	}

	if m.Mark != nil && m.Mark.Ask != "" {
		fmt.Fprintf(&sb, "> ❓ **Waiting for input**: %s\n\n", m.Mark.Ask)
	}

	return sb.String()
}

// FormatTopicsMarkdown renders the list of blackboard topics from TOPICS-STATE and TOPICS-JOURNAL.
func FormatTopicsMarkdown(topics []TopicSnapshot) string {
	if len(topics) == 0 {
		return "No topic entries have been published to `TOPICS-STATE` or `TOPICS-JOURNAL` yet.\n"
	}
	var sb strings.Builder
	sb.WriteString("## 📋 Kube-Agents Blackboard Topics\n\n")
	sb.WriteString("| Topic | Class | Subject | Last Writer | Updated At | Summary Preview |\n")
	sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
	for _, t := range topics {
		writer := t.WriterSession
		if t.WriterProfile != "" {
			writer += " (" + t.WriterProfile + ")"
		}
		preview := truncateText(strings.ReplaceAll(t.Summary, "\n", " "), topicSummaryPreviewChars)
		fmt.Fprintf(&sb, "| **`%s`** | `%s` | `%s` | `%s` | `%s` | %s |\n",
			t.Topic,
			t.Class,
			t.Subject,
			firstNonEmptyStr(writer, "—"),
			t.UpdatedAt.Format(time.RFC3339),
			escapeTableCell(firstNonEmptyStr(preview, "—")),
		)
	}
	return sb.String()
}

// FormatTopicDetailMarkdown renders the full summary and pretty-printed JSON payload of a topic.
func FormatTopicDetailMarkdown(snap *TopicSnapshot) string {
	if snap == nil {
		return ""
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "## 📄 Topic: `%s` (`%s`)\n\n", snap.Topic, snap.Subject)
	fmt.Fprintf(&sb, "- **Stream / Class**: `%s` (`%s`)\n", snap.Stream, snap.Class)
	fmt.Fprintf(&sb, "- **Written By**: `%s`", snap.WriterSession)
	if snap.WriterProfile != "" {
		fmt.Fprintf(&sb, " (profile `%s`)", snap.WriterProfile)
	}
	fmt.Fprintf(&sb, " at `%s`\n", snap.UpdatedAt.Format(time.RFC3339))
	if snap.CorrelationID != "" {
		fmt.Fprintf(&sb, "- **Correlation ID**: `%s`\n", snap.CorrelationID)
	}
	if snap.TaskID != "" {
		fmt.Fprintf(&sb, "- **Written During Task**: `%s`\n", snap.TaskID)
	}
	sb.WriteString("\n")
	if snap.Summary != "" {
		sb.WriteString("### Summary\n\n")
		sb.WriteString(snap.Summary + "\n\n")
	}
	if len(snap.Data) > 0 {
		sb.WriteString("### Structured Data\n\n```json\n")
		var pretty bytes.Buffer
		if json.Indent(&pretty, snap.Data, "", "  ") == nil {
			sb.WriteString(pretty.String())
		} else {
			sb.WriteString(string(snap.Data))
		}
		sb.WriteString("\n```\n")
	}
	return sb.String()
}

// FormatPRInspectionMarkdown renders a GitHub PR with clickable file:// diff links for Jetski.
func FormatPRInspectionMarkdown(insp *PRInspection) string {
	if insp == nil {
		return ""
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "## 🔀 Pull Request [%s#%d](%s): %s\n\n", insp.Repo, insp.Number, insp.URL, insp.Title)
	fmt.Fprintf(&sb, "- **State**: `%s` | **Author**: `%s` | **Branch**: `%s` → `%s`\n",
		insp.State, firstNonEmptyStr(insp.Author, "—"), insp.HeadRef, insp.BaseRef)
	fmt.Fprintf(&sb, "- **Changes**: `+ %d` / `- %d` across `%d` file(s)\n",
		insp.Additions, insp.Deletions, insp.ChangedFiles)
	if insp.DiffFilePath != "" {
		fmt.Fprintf(&sb, "- **Native Jetski Unified Diff File**: [%s](%s%s)\n",
			filepath.Base(insp.DiffFilePath), fileSchemePrefix, insp.DiffFilePath)
	}
	if insp.SummaryFilePath != "" {
		fmt.Fprintf(&sb, "- **Local PR Summary Markdown**: [%s](%s%s)\n",
			filepath.Base(insp.SummaryFilePath), fileSchemePrefix, insp.SummaryFilePath)
	}
	sb.WriteString("\n")

	if strings.TrimSpace(insp.Body) != "" {
		sb.WriteString("### 📝 PR Description\n\n")
		sb.WriteString(strings.TrimSpace(insp.Body) + "\n\n")
	}

	if len(insp.Files) > 0 {
		sb.WriteString("### 📂 Changed Files (Click Patch Link to Open in Jetski Viewer)\n\n")
		sb.WriteString("| File | Status | Additions | Deletions | Native Patch Link |\n")
		sb.WriteString("| :--- | :--- | ---: | ---: | :--- |\n")
		for _, f := range insp.Files {
			patchLink := "—"
			if f.PatchPath != "" {
				patchLink = fmt.Sprintf("[%s](%s%s)", filepath.Base(f.PatchPath), fileSchemePrefix, f.PatchPath)
			}
			fmt.Fprintf(&sb, "| `%s` | `%s` | `+%d` | `-%d` | %s |\n",
				f.Path, firstNonEmptyStr(f.Status, "modified"), f.Additions, f.Deletions, patchLink)
		}
		sb.WriteString("\n")
	}

	if strings.TrimSpace(insp.UnifiedDiff) != "" {
		sb.WriteString("### 🔍 Unified Diff\n\n```diff\n")
		diffText := insp.UnifiedDiff
		if len(diffText) > maxInlineDiffBytes {
			diffText = diffText[:maxInlineDiffBytes] + "\n... (diff truncated inline; open full diff at " + fileSchemePrefix + insp.DiffFilePath + ")\n"
		}
		sb.WriteString(diffText)
		if !strings.HasSuffix(diffText, "\n") {
			sb.WriteString("\n")
		}
		sb.WriteString("```\n")
	}
	return sb.String()
}

func collectOutcomePRs(outcome *TurnOutcome) []string {
	var out []string
	add := func(links []string) {
		for _, l := range links {
			if !containsString(out, l) {
				out = append(out, l)
			}
		}
	}
	if outcome.PrimaryTask != nil {
		add(outcome.PrimaryTask.PRLinks)
	}
	for _, c := range outcome.ChildTasks {
		add(c.PRLinks)
	}
	if outcome.WakeTask != nil {
		add(outcome.WakeTask.PRLinks)
	}
	return out
}

func formatTaskStateBadge(st lib.TaskState) string {
	switch st {
	case lib.StateCompleted:
		return badgeCompleted
	case lib.StateWorking:
		return badgeWorking
	case lib.StateSubmitted:
		return badgeSubmitted
	case lib.StateInputRequired:
		return badgeInputRequired
	case lib.StateFailed:
		return badgeFailed
	case lib.StateCanceled:
		return badgeCanceled
	case lib.StateRejected:
		return badgeRejected
	default:
		return fmt.Sprintf("`%s`", st)
	}
}

func formatSeverityIcon(sev string) string {
	switch sev {
	case severityGood:
		return "✅"
	case severityWarn:
		return "⚠️"
	case severityBad:
		return "❌"
	case severityInfo:
		return "ℹ️"
	default:
		return "•"
	}
}

func escapeTableCell(s string) string {
	s = strings.ReplaceAll(strings.TrimSpace(s), "\n", " ")
	return strings.ReplaceAll(s, "|", "\\|")
}

func formatByteCount(b int64) string {
	if b < 0 {
		return "unlimited"
	}
	if b >= bytesPerMB {
		return fmt.Sprintf("%.1f MB", float64(b)/float64(bytesPerMB))
	}
	if b >= bytesPerKB {
		return fmt.Sprintf("%.1f KB", float64(b)/float64(bytesPerKB))
	}
	return fmt.Sprintf("%d B", b)
}
