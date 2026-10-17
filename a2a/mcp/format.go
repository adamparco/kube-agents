package mcp

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
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

	sb.WriteString("### 🤖 Registered Agent Cards (`DIRECTORY`)\n\n")
	if len(rep.AgentCards) == 0 {
		sb.WriteString("_No agent cards currently cached on the `DIRECTORY` stream (`a2a.agents.>`)._\n")
	} else {
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

var (
	legacyStepPrefixRe        = regexp.MustCompile(`^Step\s+\d+:\s*`)
	legacyPlanPrefixRe        = regexp.MustCompile(`(?i)^Planning\s*&\s*executing\s+\d+\s+tool\s+call\(s\)\s*[—-]\s*`)
	legacyActionPrefixRe      = regexp.MustCompile(`^→\s*Action:\s*`)
	legacySynthPrefixRe       = regexp.MustCompile(`(?i)^Synthesizing findings from \d+ completed tool call\(s\) into final response\.?$`)
	legacyKeyBracketSufRe     = regexp.MustCompile(`\s*\[[A-Za-z0-9_,\s-]+\]$`)
	kanbanProgressDoneRe      = regexp.MustCompile(`(?i)\b(\d+/\d+)\s+done\b`)
	kanbanStillRunningRe      = regexp.MustCompile(`(?i)\b(still running|wait \d+\s*s|re-check|heartbeating)\b`)
	preflightDiagRe           = regexp.MustCompile(`(?i)\bpreflight\b.*\bdiagnostics\b`)
	nodePoolInspectRe         = regexp.MustCompile(`(?i)\bnode\s*pools?\b.*\b(pods?|nodes?|events?)\b`)
	conversationalStartRe     = regexp.MustCompile(`(?i)^(let's |let me |now |i will |i'll |i've |i have |first[, ]|next[, ]|okay[, ]|ok[, .]|all \d+ |both |delegating )`)
	shapedStringPlaceholderRe = regexp.MustCompile(`^<string,\s*(\d+)\s*chars>$`)
)

const (
	cardResultsRawHeader = "Specialist card results (raw):"
	silentResultFallback = "_The agent completed this turn without a textual reply._"
	synthFinalStepTitle  = "Synthesizing findings into final response."
	// streamingStepContinuePrefix marks an intermediate (task still running) tool result frame.
	streamingStepContinuePrefix = "STREAMING_STEP_CONTINUE:"
	maxStepActivitiesShown      = 6
	cardHeadlineMaxChars        = 64
	cardThoughtMaxChars         = 400
	mergedThoughtMaxChars       = 420
)

// StepDetail holds the clean high-level action title and the expandable details
// (agent/task provenance, reasoning thought, delegation details, and tool call activities)
// for a single step in a kube-agents turn.
type StepDetail struct {
	Title          string         `json:"title"`
	Thought        string         `json:"thought,omitempty"`
	Agent          string         `json:"agent,omitempty"`
	TaskID         string         `json:"taskId,omitempty"`
	DelegatedTo    string         `json:"delegatedTo,omitempty"`
	ChildTaskID    string         `json:"childTaskId,omitempty"`
	DelegatePrompt string         `json:"delegatePrompt,omitempty"`
	Activities     []ActivityItem `json:"activities,omitempty"`
}

// FormatTurnOutcomeMarkdown renders a turn outcome using the default clean, collapsed-thinking Jetski UX.
func FormatTurnOutcomeMarkdown(outcome *TurnOutcome) string {
	return FormatTurnOutcomeMarkdownWithOptions(outcome, false)
}

// FormatTurnOutcomeMarkdownWithOptions renders a turn outcome.
// When debug is false (the default), it renders a super clean, crisp native Jetski view:
// a collapsed-by-default <details><summary>Thought for Xs</summary>...</details> block followed
// directly by the final answer, hiding tool call tables, timings, and task metadata.
// When debug is true, it includes the full Tool Activity Stream table (tool calls, timings, inputs),
// task/context IDs, gateway notices, and delegation flow diagrams for debugging kube-agents itself.
func FormatTurnOutcomeMarkdownWithOptions(outcome *TurnOutcome, debug bool) string {
	if outcome == nil {
		return ""
	}
	if !debug {
		return formatCleanTurnOutcomeMarkdown(outcome)
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
		sb.WriteString(FormatTaskRecordMarkdownWithOptions(outcome.PrimaryTask, "Primary Task", true))
	}

	for i, child := range outcome.ChildTasks {
		sb.WriteString("\n---\n")
		sb.WriteString(FormatTaskRecordMarkdownWithOptions(child, fmt.Sprintf("Delegated Child Task #%d", i+1), true))
	}

	if outcome.WakeTask != nil {
		sb.WriteString("\n---\n")
		sb.WriteString(FormatTaskRecordMarkdownWithOptions(outcome.WakeTask, "Session Wake Synthesis Task", true))
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

// CollectOutcomeSteps extracts a clean, deduplicated chronological list of human-readable
// step titles taken across the primary task, delegated child tasks, and wake synthesis task.
func CollectOutcomeSteps(outcome *TurnOutcome) []string {
	details := CollectOutcomeStepDetails(outcome)
	if len(details) == 0 {
		return nil
	}
	steps := make([]string, 0, len(details))
	for _, d := range details {
		steps = append(steps, d.Title)
	}
	return steps
}

// CollectOutcomeStepDetails extracts a clean, deduplicated chronological list of StepDetail items
// (each with a concise action Title, plus Agent/TaskID, Thought reasoning, Delegation info, and
// matched Tool Activity items for the expandable row body) across the primary task, delegated
// child tasks, and wake synthesis task.
func CollectOutcomeStepDetails(outcome *TurnOutcome) []StepDetail {
	if outcome == nil {
		return nil
	}
	var rawDetails []StepDetail
	appendDetail := func(sd StepDetail) {
		sd.Title = strings.TrimSpace(sd.Title)
		if sd.Title == "" {
			return
		}
		isSynth := strings.EqualFold(strings.TrimSuffix(sd.Title, "."), strings.TrimSuffix(synthFinalStepTitle, "."))
		if isSynth {
			for i := 0; i < len(rawDetails); i++ {
				if strings.EqualFold(strings.TrimSuffix(rawDetails[i].Title, "."), strings.TrimSuffix(sd.Title, ".")) {
					rawDetails = append(rawDetails[:i], rawDetails[i+1:]...)
					break
				}
			}
		}
		if len(rawDetails) > 0 {
			last := &rawDetails[len(rawDetails)-1]
			if strings.EqualFold(strings.TrimSuffix(last.Title, "."), strings.TrimSuffix(sd.Title, ".")) {
				if last.Thought == "" && sd.Thought != "" {
					last.Thought = sd.Thought
				} else if sd.Thought != "" {
					last.Thought = mergeThoughts(last.Thought, sd.Thought)
				}
				if last.Agent == "" && sd.Agent != "" {
					last.Agent = sd.Agent
				}
				if last.TaskID == "" && sd.TaskID != "" {
					last.TaskID = sd.TaskID
				}
				if last.DelegatedTo == "" && sd.DelegatedTo != "" {
					last.DelegatedTo = sd.DelegatedTo
				}
				if last.ChildTaskID == "" && sd.ChildTaskID != "" {
					last.ChildTaskID = sd.ChildTaskID
				}
				if last.DelegatePrompt == "" && sd.DelegatePrompt != "" {
					last.DelegatePrompt = sd.DelegatePrompt
				}
				last.Activities = mergeActivityItems(last.Activities, sd.Activities)
				return
			}
		}
		rawDetails = append(rawDetails, sd)
	}

	for _, sd := range collectRecStepDetails(outcome.PrimaryTask) {
		appendDetail(sd)
	}
	primaryID := ""
	if outcome.PrimaryTask != nil {
		primaryID = outcome.PrimaryTask.TaskID
	}
	for _, sd := range collectCardStepDetails(outcome.CardTasks, primaryID) {
		appendDetail(sd)
	}
	for idx, d := range outcome.PrimaryTaskDelegates() {
		if d.Addressee != "" {
			childID := d.ChildTaskID
			if childID == "" && idx < len(outcome.ChildTasks) && outcome.ChildTasks[idx] != nil {
				childID = outcome.ChildTasks[idx].TaskID
			}
			var agent, taskID string
			if outcome.PrimaryTask != nil {
				agent = outcome.PrimaryTask.Addressee
				taskID = outcome.PrimaryTask.TaskID
			}
			appendDetail(StepDetail{
				Title:          fmt.Sprintf("Delegating task to %s.", d.Addressee),
				Agent:          agent,
				TaskID:         taskID,
				DelegatedTo:    d.Addressee,
				ChildTaskID:    childID,
				DelegatePrompt: strings.TrimSpace(d.Text),
			})
		}
	}
	for _, child := range outcome.ChildTasks {
		for _, sd := range collectRecStepDetails(child) {
			appendDetail(sd)
		}
		for _, sd := range collectCardStepDetails(outcome.CardTasks, child.TaskID) {
			appendDetail(sd)
		}
	}
	for _, sd := range collectRecStepDetails(outcome.WakeTask) {
		appendDetail(sd)
	}

	// Filter out premature "Synthesizing findings into final response." if more steps follow it
	// or if the task tree is still actively running with other steps in progress.
	if len(rawDetails) <= 1 {
		return rawDetails
	}
	var filtered []StepDetail
	for i, sd := range rawDetails {
		isSynth := strings.EqualFold(strings.TrimSuffix(sd.Title, "."), strings.TrimSuffix(synthFinalStepTitle, "."))
		if isSynth {
			isLast := i == len(rawDetails)-1
			if !isLast || outcome.TimedOut {
				continue
			}
		}
		filtered = append(filtered, sd)
	}
	return filtered
}

// collectCardStepDetails renders Kanban specialist card tasks fanned out by parentID as steps:
// one row per card, titled by the card's result headline (or prompt while still running), with the
// card's result text as the expandable Thought so long platform fan-outs show real progress instead
// of a silent multi-minute "waiting" gap.
func collectCardStepDetails(cards []*TaskRecord, parentID string) []StepDetail {
	if parentID == "" {
		return nil
	}
	var out []StepDetail
	for _, card := range cards {
		if card == nil || card.CardParentTaskID != parentID {
			continue
		}
		headline := cardHeadline(card)
		var title, thought string
		switch {
		case card.Terminal && (card.State == lib.StateFailed || card.State == lib.StateRejected || card.State == lib.StateCanceled):
			title = fmt.Sprintf("Specialist check %s: %s.", card.State, headline)
			thought = firstNonEmptyStr(card.StatusText, card.ResultText, card.ProgressText)
		case card.Terminal:
			title = fmt.Sprintf("Specialist check completed: %s.", headline)
			thought = firstNonEmptyStr(card.ResultText, card.ProgressText, card.StatusText)
		default:
			title = fmt.Sprintf("Specialist check running: %s.", headline)
			thought = firstNonEmptyStr(card.ProgressText, card.PromptText)
		}
		thought = strings.ReplaceAll(thought, "**", "")
		thought = truncateText(strings.Join(strings.Fields(thought), " "), cardThoughtMaxChars)
		out = append(out, StepDetail{
			Title:      title,
			Thought:    thought,
			Agent:      card.Addressee,
			TaskID:     card.TaskID,
			Activities: append([]ActivityItem(nil), card.Activities...),
		})
	}
	return out
}

func cardHeadline(card *TaskRecord) string {
	if card.ResultChat != nil && strings.TrimSpace(card.ResultChat.Headline) != "" {
		return cleanCardHeadline(card.ResultChat.Headline)
	}
	if card.ProgressChat != nil && strings.TrimSpace(card.ProgressChat.Headline) != "" {
		return cleanCardHeadline(card.ProgressChat.Headline)
	}
	for _, cand := range []string{card.ResultText, card.PromptText, card.ProgressText} {
		for _, line := range strings.Split(cand, "\n") {
			line = strings.TrimSpace(line)
			if line != "" {
				return cleanCardHeadline(line)
			}
		}
	}
	return card.TaskID
}

func cleanCardHeadline(s string) string {
	s = strings.TrimSpace(s)
	s = strings.Trim(s, "*_#> ")
	s = strings.ReplaceAll(s, "**", "")
	// Keep the verdict, drop the elaboration: "X is healthy — 1/1 node Ready, ..." -> "X is healthy".
	for _, sep := range []string{" — ", " - ", ": ", "; "} {
		if idx := strings.Index(s, sep); idx > 12 {
			s = s[:idx]
			break
		}
	}
	s = strings.TrimSuffix(strings.TrimSpace(s), ".")
	return truncateText(s, cardHeadlineMaxChars)
}

// mergeThoughts appends a new thought to an existing one for a deduplicated step, bounded so a
// step that absorbs many in-process worker thoughts (platform fan-outs run card workers in the same
// hermes session) keeps only the most recent reasoning instead of growing without limit.
func mergeThoughts(existing, added string) string {
	added = strings.TrimSpace(added)
	if added == "" {
		return existing
	}
	if existing == "" {
		return added
	}
	if strings.Contains(existing, added) {
		return existing
	}
	merged := existing + " " + added
	if len(merged) <= mergedThoughtMaxChars {
		return merged
	}
	tail := merged[len(merged)-mergedThoughtMaxChars:]
	if idx := strings.Index(tail, ". "); idx >= 0 && idx < mergedThoughtMaxChars/2 {
		tail = tail[idx+2:]
	}
	return "… " + tail
}

func collectRecStepDetails(rec *TaskRecord) []StepDetail {
	if rec == nil {
		return nil
	}
	var steps []StepDetail
	if raw := strings.TrimSpace(rec.Thinking); raw != "" {
		steps = parseAndCleanThinkingSteps(raw)
	} else if synth := strings.TrimSpace(SynthesizeThinkingFromActivities(rec)); synth != "" {
		steps = parseAndCleanThinkingSteps(synth)
	}
	for i := range steps {
		steps[i].Agent = rec.Addressee
		steps[i].TaskID = rec.TaskID
	}
	if len(steps) == 0 || len(rec.Activities) == 0 {
		return steps
	}

	lastIdx := 0
	for _, act := range rec.Activities {
		matchedIdx := -1
		for j := lastIdx; j < len(steps); j++ {
			if exactActivityMatchesStepTitle(act, steps[j].Title) {
				matchedIdx = j
				break
			}
		}
		if matchedIdx < 0 {
			for j := lastIdx; j < len(steps); j++ {
				if broadActivityMatchesStepTitle(act, steps[j].Title) {
					matchedIdx = j
					break
				}
			}
		}
		if matchedIdx < 0 {
			for j := 0; j < lastIdx; j++ {
				if exactActivityMatchesStepTitle(act, steps[j].Title) {
					matchedIdx = j
					break
				}
			}
		}
		if matchedIdx < 0 {
			for j := 0; j < lastIdx; j++ {
				if broadActivityMatchesStepTitle(act, steps[j].Title) {
					matchedIdx = j
					break
				}
			}
		}
		if matchedIdx < 0 {
			matchedIdx = lastIdx
		}
		if matchedIdx >= lastIdx {
			lastIdx = matchedIdx
		}
		steps[matchedIdx].Activities = mergeActivityItems(steps[matchedIdx].Activities, []ActivityItem{act})
	}
	return steps
}

func mergeActivityItems(existing []ActivityItem, added []ActivityItem) []ActivityItem {
	for _, item := range added {
		dup := false
		for _, cur := range existing {
			if item.CallID != "" && cur.CallID == item.CallID {
				dup = true
				break
			}
			if item.CallID == "" && cur.Tool == item.Tool && cur.Input == item.Input && cur.DurationMs == item.DurationMs && cur.At == item.At {
				dup = true
				break
			}
		}
		if !dup {
			existing = append(existing, item)
		}
	}
	return existing
}

func exactActivityMatchesStepTitle(act ActivityItem, stepTitle string) bool {
	stepClean := strings.TrimSuffix(strings.TrimSpace(stepTitle), ".")
	if strings.HasPrefix(strings.ToLower(stepClean), "specialist check ") && strings.HasPrefix(act.Worker, "card:") {
		if idx := strings.Index(act.Worker, "("); idx >= 0 {
			clusterOrTitle := strings.TrimSuffix(strings.TrimSpace(act.Worker[idx+1:]), ")")
			if clusterOrTitle != "" && strings.Contains(strings.ToLower(stepClean), strings.ToLower(clusterOrTitle)) {
				return true
			}
		}
	}
	for _, cand := range activityCandidateStepTitles(act) {
		candClean := strings.TrimSuffix(strings.TrimSpace(cand), ".")
		if strings.EqualFold(stepClean, candClean) {
			return true
		}
	}
	return false
}

func broadActivityMatchesStepTitle(act ActivityItem, stepTitle string) bool {
	lowerTitle := strings.ToLower(stepTitle)
	for _, toolName := range expandActivityToolNames(act) {
		switch {
		case toolName == "terminal" || toolName == "kubectl" || toolName == "run_kubectl" || toolName == "bash":
			if strings.Contains(lowerTitle, "kubernetes") ||
				strings.Contains(lowerTitle, "diagnostics") ||
				strings.Contains(lowerTitle, "node") ||
				strings.Contains(lowerTitle, "pod") ||
				strings.Contains(lowerTitle, "manifest") ||
				strings.Contains(lowerTitle, "repository") ||
				strings.Contains(lowerTitle, "pull request") ||
				strings.Contains(lowerTitle, "preflight") {
				return true
			}
		case toolName == "read_file":
			if strings.HasPrefix(lowerTitle, "reading ") {
				return true
			}
		case toolName == "patch" || toolName == "edit_file" || toolName == "replace_in_file":
			if strings.HasPrefix(lowerTitle, "editing ") {
				return true
			}
		case toolName == "write_file" || toolName == "create_file":
			if strings.HasPrefix(lowerTitle, "writing ") {
				return true
			}
		case toolName == "search_files" || toolName == "find_by_name" || toolName == "list_dir" || toolName == "glob":
			if strings.HasPrefix(lowerTitle, "inspecting workspace") || strings.HasPrefix(lowerTitle, "inspecting files") {
				return true
			}
		case strings.HasPrefix(toolName, "kanban_"):
			if strings.Contains(lowerTitle, "kanban") || strings.Contains(lowerTitle, "specialist") {
				return true
			}
		case strings.HasPrefix(toolName, "mcp__gke__") || strings.HasPrefix(toolName, "mcp__platform_control__"):
			if strings.Contains(lowerTitle, "gke") || strings.Contains(lowerTitle, "cluster") || strings.Contains(lowerTitle, "platform control") {
				return true
			}
		}
	}
	return false
}

func expandActivityToolNames(act ActivityItem) []string {
	if act.Tool == "tool_call" && strings.TrimSpace(act.Input) != "" {
		var wrapper struct {
			Calls []struct {
				Name string `json:"name"`
			} `json:"calls"`
		}
		if json.Unmarshal([]byte(act.Input), &wrapper) == nil && len(wrapper.Calls) > 0 {
			var names []string
			for _, c := range wrapper.Calls {
				if n := strings.TrimSpace(c.Name); n != "" {
					names = append(names, n)
				}
			}
			if len(names) > 0 {
				return names
			}
		}
	}
	if act.Tool != "" {
		return []string{act.Tool}
	}
	return nil
}

func activityCandidateStepTitles(act ActivityItem) []string {
	var titles []string
	for _, toolName := range expandActivityToolNames(act) {
		titles = append(titles, toolNameToStepTitle(toolName))
	}
	return titles
}

func toolNameToStepTitle(toolName string) string {
	switch {
	case toolName == "mcp__gke__list_clusters":
		return "Listing GKE clusters in the fleet."
	case toolName == "mcp__gke__get_cluster":
		return "Inspecting GKE cluster configuration."
	case strings.HasPrefix(toolName, "mcp__gke__"):
		return "Querying GKE cluster state."
	case toolName == "mcp__platform_control__list_cluster_profiles":
		return "Listing GKE cluster profiles in the fleet."
	case strings.HasPrefix(toolName, "mcp__platform_control__"):
		return "Querying platform control state."
	case toolName == "read_file":
		return "Reading file in the workspace."
	case toolName == "search_files" || toolName == "find_by_name" || toolName == "list_dir" || toolName == "glob":
		return "Inspecting files in the workspace."
	case toolName == "patch" || toolName == "edit_file" || toolName == "replace_in_file":
		return "Editing a file in the agent workspace."
	case toolName == "write_file" || toolName == "create_file":
		return "Writing a file in the agent workspace."
	case toolName == "terminal" || toolName == "kubectl" || toolName == "run_kubectl" || toolName == "bash":
		return "Running cluster and workspace diagnostics."
	case toolName == "kanban_create":
		return "Delegating task to specialist agent."
	case strings.HasPrefix(toolName, "kanban_"):
		return "Updating task status on the Kanban board."
	case toolName == "tool_search":
		return "Searching available tools."
	case toolName == "tool_describe":
		return "Inspecting tool schema."
	case toolName == "execute_code":
		return "Running analysis script."
	case toolName == "skill_view":
		return "Loading skill instructions."
	case toolName == "mcp__a2a__delegate":
		return "Delegating task to platform."
	case strings.HasPrefix(toolName, "mcp__a2a__"):
		return "Communicating over the A2A bus."
	case strings.HasPrefix(toolName, "mcp__developer_knowledge__"):
		return "Searching documentation in Developer Knowledge."
	case toolName == "web_search":
		return "Searching the web."
	case toolName == "web_extract" || toolName == "web_fetch" || toolName == "read_url_content":
		return "Reading web page content."
	default:
		return fmt.Sprintf("Running %s.", toolName)
	}
}

// PrimaryTaskDelegates returns the delegates slice on PrimaryTask if non-nil.
func (o *TurnOutcome) PrimaryTaskDelegates() []DelegateItem {
	if o == nil || o.PrimaryTask == nil {
		return nil
	}
	return o.PrimaryTask.Delegates
}

func countCardProgress(cards []*TaskRecord) (done int, total int) {
	for _, c := range cards {
		if c == nil {
			continue
		}
		total++
		if c.Terminal {
			done++
		}
	}
	return done, total
}

// KanbanCardSummary represents a specialist Kanban card observed via tool calls or thinking updates.
type KanbanCardSummary struct {
	ID        string
	Title     string
	Cluster   string
	Worker    string
	SessionID string
	Status    string
	Verdict   string
	Summary   string
}

func collectOutcomeActivities(outcome *TurnOutcome) []ActivityItem {
	if outcome == nil {
		return nil
	}
	var out []ActivityItem
	if outcome.PrimaryTask != nil {
		out = append(out, outcome.PrimaryTask.Activities...)
	}
	for _, c := range outcome.ChildTasks {
		if c != nil {
			out = append(out, c.Activities...)
		}
	}
	for _, c := range outcome.CardTasks {
		if c != nil {
			out = append(out, c.Activities...)
		}
	}
	if outcome.WakeTask != nil {
		out = append(out, outcome.WakeTask.Activities...)
	}
	return out
}

func collectDeltaActivities(outcome *TurnOutcome) []ActivityItem {
	all := collectOutcomeActivities(outcome)
	if len(all) == 0 {
		return nil
	}
	reported := outcome.ReportedActivityCount
	if reported <= 0 || reported > len(all) {
		reported = len(all)
	}
	prev := outcome.PrevActivityCount
	if prev < 0 {
		prev = 0
	}
	if prev > reported {
		prev = reported
	}
	return all[prev:reported]
}

var (
	kanbanResultIDRe       = regexp.MustCompile(`\bid=([A-Za-z0-9_-]+)`)
	specialistStepHeaderRe = regexp.MustCompile(`(?i)^Specialist check completed:\s*(.+?)\s*\(([^()—-]+?)\s*[—-]\s*(\d+)/(\d+)\s+cards\s+done\)\.?$`)
	profileRegionSuffixRe  = regexp.MustCompile(`-(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-[a-z0-9-]+$`)
)

func parseActivityArgMap(raw string) map[string]any {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" || !strings.HasPrefix(trimmed, "{") {
		return nil
	}
	var m map[string]any
	if json.Unmarshal([]byte(trimmed), &m) != nil {
		return nil
	}
	return m
}

func firstNonEmptyArgString(m map[string]any, keys ...string) string {
	if len(m) == 0 {
		return ""
	}
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok {
				s = strings.TrimSpace(s)
				if s != "" && !strings.HasPrefix(s, "<string,") && s != "[redacted]" {
					return s
				}
			}
		}
	}
	return ""
}

func summarizeKanbanMetadata(args map[string]any) string {
	meta, ok := args["metadata"].(map[string]any)
	if !ok || len(meta) == 0 {
		return ""
	}
	return formatArgMapSummaryWithLimit(meta, 80)
}

func extractClusterFromAssignee(assignee string) string {
	assignee = strings.ToLower(strings.TrimSpace(assignee))
	if strings.HasPrefix(assignee, "cluster-") {
		rest := strings.TrimPrefix(assignee, "cluster-")
		return profileRegionSuffixRe.ReplaceAllString(rest, "")
	}
	return ""
}

func formatActivityAtTime(at string) string {
	at = strings.TrimSpace(at)
	if at == "" {
		return ""
	}
	if t, err := time.Parse(time.RFC3339Nano, at); err == nil {
		return t.UTC().Format("15:04:05")
	}
	if t, err := time.Parse(time.RFC3339, at); err == nil {
		return t.UTC().Format("15:04:05")
	}
	return at
}

func extractKanbanCardsFromTask(rec *TaskRecord) (cards []KanbanCardSummary, doneCount int, runningCount int, totalCount int, latestNote string) {
	if rec == nil {
		return nil, 0, 0, 0, ""
	}
	byID := make(map[string]*KanbanCardSummary)
	var order []string
	ensureCard := func(id string) *KanbanCardSummary {
		id = strings.TrimSpace(id)
		if id == "" {
			return nil
		}
		if c, ok := byID[id]; ok {
			return c
		}
		c := &KanbanCardSummary{
			ID:     id,
			Status: "pending",
		}
		byID[id] = c
		order = append(order, id)
		return c
	}

	var anonCreateIdx int
	for _, act := range rec.Activities {
		args := parseActivityArgMap(act.Input)
		switch act.Tool {
		case "kanban_create":
			cardID := firstNonEmptyArgString(args, "task_id", "card_id", "id")
			if cardID == "" && act.ResultPreview != "" {
				if cid := extractCardIDFromPreview(act.ResultPreview); cid != "" {
					cardID = cid
				} else if m := kanbanResultIDRe.FindStringSubmatch(act.ResultPreview); len(m) == 2 {
					cardID = m[1]
				}
			}
			if cardID == "" {
				anonCreateIdx++
				cardID = fmt.Sprintf("card_%d", anonCreateIdx)
			}
			c := ensureCard(cardID)
			if c != nil {
				if t := firstNonEmptyArgString(args, "title"); t != "" {
					c.Title = t
				}
				if as := firstNonEmptyArgString(args, "assignee"); as != "" && c.Worker == "" {
					c.Worker = as
				}
				if cl := firstNonEmptyArgString(args, "cluster", "cluster_name"); cl != "" {
					c.Cluster = cl
				} else if cl := extractClusterFromAssignee(c.Worker); cl != "" {
					c.Cluster = cl
				}
			}
		case "kanban_claim":
			cardID := firstNonEmptyArgString(args, "task_id", "card_id", "id")
			if c := ensureCard(cardID); c != nil {
				if c.Status == "pending" {
					c.Status = "running"
				}
				if act.SessionID != "" {
					c.SessionID = act.SessionID
				}
				if act.Worker != "" {
					c.Worker = act.Worker
				}
			}
		case "kanban_heartbeat":
			cardID := firstNonEmptyArgString(args, "task_id", "card_id", "id")
			note := firstNonEmptyArgString(args, "note", "status", "message")
			if note != "" && !strings.HasPrefix(note, "<string,") {
				if cardID != "" {
					latestNote = fmt.Sprintf("Card %s: %s", cardID, note)
				} else {
					latestNote = note
				}
			}
			if c := ensureCard(cardID); c != nil {
				if c.Status == "pending" {
					c.Status = "running"
				}
				if act.SessionID != "" {
					c.SessionID = act.SessionID
				}
				if note != "" && !strings.HasPrefix(note, "<string,") && c.Summary == "" {
					c.Summary = note
				}
			}
		case "kanban_done", "kanban_complete":
			cardID := firstNonEmptyArgString(args, "task_id", "card_id", "id")
			var c *KanbanCardSummary
			if cardID != "" {
				c = ensureCard(cardID)
			} else {
				for _, id := range order {
					cand := byID[id]
					if cand != nil && cand.Status != "done" && cand.Status != "failed" {
						if act.SessionID != "" && cand.SessionID == act.SessionID {
							c = cand
							break
						}
						if c == nil {
							c = cand
						}
					}
				}
			}
			if c != nil {
				c.Status = "done"
				if act.SessionID != "" {
					c.SessionID = act.SessionID
				}
				if v := firstNonEmptyArgString(args, "verdict", "status"); v != "" {
					c.Verdict = v
				}
				if s := firstNonEmptyArgString(args, "summary", "result", "note"); s != "" {
					c.Summary = s
				} else if c.Summary == "" {
					if mSum := summarizeKanbanMetadata(args); mSum != "" {
						c.Summary = mSum
					}
				}
			}
		case "kanban_fail":
			cardID := firstNonEmptyArgString(args, "task_id", "card_id", "id")
			if c := ensureCard(cardID); c != nil {
				c.Status = "failed"
				c.Verdict = "failed"
				if act.SessionID != "" {
					c.SessionID = act.SessionID
				}
				if r := firstNonEmptyArgString(args, "reason", "error", "summary"); r != "" && !strings.HasPrefix(r, "<string,") {
					c.Summary = r
				}
			}
		}
		if strings.HasPrefix(act.Worker, "card:") && act.SessionID != "" {
			rest := strings.TrimPrefix(act.Worker, "card:")
			if idx := strings.IndexByte(rest, ' '); idx > 0 {
				rest = rest[:idx]
			}
			if c, ok := byID[rest]; ok && c != nil {
				c.SessionID = act.SessionID
				if c.Status == "pending" {
					c.Status = "running"
				}
			}
		}
	}

	// Also fold in Specialist check completed thinking lines emitted by waitForKanbanCards (A1).
	for _, sub := range strings.Split(rec.Thinking, "\n") {
		sub = strings.TrimSpace(sub)
		if sub == "" {
			continue
		}
		head := sub
		detail := ""
		if tabIdx := strings.IndexByte(sub, '\t'); tabIdx >= 0 {
			head = strings.TrimSpace(sub[:tabIdx])
			detail = strings.TrimSpace(sub[tabIdx+1:])
		}
		if m := specialistStepHeaderRe.FindStringSubmatch(head); len(m) == 5 {
			shortTitle := cleanShortCardTitle(m[1])
			verdict := strings.ToLower(strings.TrimSpace(m[2]))
			var matched *KanbanCardSummary
			for _, id := range order {
				c := byID[id]
				if strings.EqualFold(c.Cluster, shortTitle) || strings.Contains(strings.ToLower(c.Title), strings.ToLower(shortTitle)) {
					matched = c
					break
				}
			}
			if matched == nil {
				matched = ensureCard(shortTitle)
				if matched != nil && matched.Title == "" {
					matched.Title = shortTitle
				}
			}
			if matched != nil {
				if verdict == "failed" {
					matched.Status = "failed"
				} else {
					matched.Status = "done"
				}
				matched.Verdict = verdict
				if detail != "" {
					matched.Summary = detail
				}
			}
		}
	}

	// Merge anonymous card_N placeholders into real t_* cards when titles/clusters match.
	var realIDs, anonIDs []string
	for _, id := range order {
		if strings.HasPrefix(id, "card_") {
			anonIDs = append(anonIDs, id)
		} else {
			realIDs = append(realIDs, id)
		}
	}
	if len(realIDs) > 0 && len(anonIDs) > 0 {
		usedAnon := make(map[string]bool)
		for i, realID := range realIDs {
			rc := byID[realID]
			if rc.Title == "" && i < len(anonIDs) {
				ac := byID[anonIDs[i]]
				rc.Title = ac.Title
				if rc.Cluster == "" {
					rc.Cluster = ac.Cluster
				}
				if rc.Worker == "" {
					rc.Worker = ac.Worker
				}
				usedAnon[anonIDs[i]] = true
			}
		}
		var filteredOrder []string
		for _, id := range order {
			if !usedAnon[id] {
				filteredOrder = append(filteredOrder, id)
			}
		}
		order = filteredOrder
	}

	for _, id := range order {
		c := byID[id]
		if c == nil {
			continue
		}
		if rec.Terminal && rec.State == lib.StateCompleted && c.Status != "done" && c.Status != "failed" {
			c.Status = "done"
		}
		totalCount++
		switch c.Status {
		case "done", "failed":
			doneCount++
		default:
			runningCount++
		}
		cards = append(cards, *c)
	}
	return cards, doneCount, runningCount, totalCount, latestNote
}

func summarizeOutcomeKanbanState(outcome *TurnOutcome) (cards []KanbanCardSummary, doneCount int, runningCount int, totalCount int, latestNote string) {
	if outcome == nil {
		return nil, 0, 0, 0, ""
	}
	if outcome.PrimaryTask != nil {
		cards, doneCount, runningCount, totalCount, latestNote = extractKanbanCardsFromTask(outcome.PrimaryTask)
	}
	if d, t := countCardProgress(outcome.CardTasks); t > totalCount {
		doneCount = d
		totalCount = t
		runningCount = t - d
	}
	return cards, doneCount, runningCount, totalCount, latestNote
}

func extractWorkerCluster(act ActivityItem) string {
	if strings.HasPrefix(act.Worker, "card:") {
		if lIdx := strings.IndexByte(act.Worker, '('); lIdx >= 0 {
			if rIdx := strings.LastIndexByte(act.Worker, ')'); rIdx > lIdx+1 {
				return strings.TrimSpace(act.Worker[lIdx+1 : rIdx])
			}
		}
	}
	args := parseActivityArgMap(act.Input)
	return firstNonEmptyArgString(args, "cluster", "cluster_name")
}

func shortActivityPath(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if idx := strings.Index(raw, "/skills/"); idx >= 0 {
		return "skill " + truncateText(strings.TrimPrefix(raw[idx+len("/skills/"):], "/"), 48)
	}
	parts := strings.Split(strings.Trim(raw, "/"), "/")
	if len(parts) > 2 {
		parts = parts[len(parts)-2:]
	}
	return truncateText(strings.Join(parts, "/"), 48)
}

func qualifyWithCluster(base, prep, cluster string) string {
	if cluster == "" || strings.Contains(strings.ToLower(base), strings.ToLower(cluster)) {
		return base
	}
	return fmt.Sprintf("%s %s %s", base, prep, cluster)
}

func describeActivityWaitStep(act ActivityItem, activeAgent string) string {
	cluster := extractWorkerCluster(act)
	args := parseActivityArgMap(act.Input)
	toolName := act.Tool
	if toolName == "tool_call" {
		names := expandActivityToolNames(act)
		if len(names) > 0 {
			toolName = names[0]
		}
	}
	if toolName == "kanban_heartbeat" {
		if note := firstNonEmptyArgString(args, "note", "status", "message"); note != "" && !strings.HasPrefix(note, "<string,") {
			return truncateText(note, 64)
		}
		return qualifyWithCluster("Updating specialist progress", "on", cluster)
	}
	if p := shortActivityPath(firstNonEmptyArgString(args, "path", "file_path", "filepath")); p != "" {
		switch toolName {
		case "read_file":
			return qualifyWithCluster("Reading "+p, "on", cluster)
		case "patch", "edit_file", "replace_in_file":
			return qualifyWithCluster("Editing "+p, "for", cluster)
		case "write_file", "create_file":
			return qualifyWithCluster("Writing "+p, "for", cluster)
		}
	}
	titles := activityCandidateStepTitles(act)
	if len(titles) > 0 && titles[0] != "" {
		return qualifyWithCluster(strings.TrimSuffix(titles[0], "."), "on", cluster)
	}
	return fmt.Sprintf("Waiting for %s to complete active step", activeAgent)
}

// FormatTurnCustomToolEnvelope builds the Jetski _custom_tool_result fields (title, markdown, result)
// for a turn outcome so that:
//  1. Each MCP tool step row in Jetski's ToolGroupCollapsible represents ONE incremental step
//     (appending 1 new item to the collapsible step list rather than creating a whole new duplicate list).
//  2. Expanding any step row (markdown) displays rich per-step details: Agent & Task ID,
//     Reasoning/Thought (or Delegation Request), and Tool Activity (tool name, arguments, timing).
//  3. While a task is still running (outcome.TimedOut == true), the LLM receives a compact
//     STREAMING_STEP_CONTINUE instruction to immediately poll the next step via task_control(action: "get")
//     without emitting intermediate chat prose, so Jetski streams live steps inside its single outer
//     collapsible group.
//  4. When the task finishes (!outcome.TimedOut && !debug), the LLM receives ONLY the final answer
//     (without a duplicate <details><summary>Thought...</summary> block) so steps only appear once
//     in the collapsible tool group above.
func FormatTurnCustomToolEnvelope(outcome *TurnOutcome, debug bool) (title string, markdown string, result string) {
	if outcome == nil {
		return "", "", ""
	}
	fullMD := FormatTurnOutcomeMarkdownWithOptions(outcome, debug)
	if outcome.PrimaryTask == nil {
		return "Executed control command", fullMD, fullMD
	}

	allDetails := CollectOutcomeStepDetails(outcome)
	sec := computeOutcomeElapsedSeconds(outcome.PrimaryTask, outcome.ChildTasks, outcome.WakeTask)
	if sec < 1 {
		sec = 1
	}

	reported := outcome.ReportedStepCount
	if reported <= 0 || reported > len(allDetails) {
		reported = len(allDetails)
	}
	prev := outcome.PrevStepCount
	if prev < 0 {
		prev = 0
	}
	if prev > reported {
		prev = reported
	}
	visibleDetails := allDetails[:reported]
	deltaDetails := visibleDetails[prev:]

	activeAgent := firstNonEmptyStr(outcome.PrimaryTask.Addressee, "platform-agent")
	activeTaskID := outcome.PrimaryTask.TaskID
	for _, c := range outcome.ChildTasks {
		if c != nil && !c.Terminal {
			activeAgent = firstNonEmptyStr(c.Addressee, activeAgent)
			activeTaskID = c.TaskID
		}
	}
	if outcome.WakeTask != nil && !outcome.WakeTask.Terminal {
		activeAgent = firstNonEmptyStr(outcome.WakeTask.Addressee, activeAgent)
		activeTaskID = outcome.WakeTask.TaskID
	}

	if outcome.TimedOut {
		var currentStep string
		var stepMD string
		deltaActs := collectDeltaActivities(outcome)
		cards, doneCards, runningCards, totalCards, latestNote := summarizeOutcomeKanbanState(outcome)

		switch {
		case len(deltaDetails) >= 1:
			lastDetail := deltaDetails[len(deltaDetails)-1]
			currentStep = strings.TrimSuffix(lastDetail.Title, ".")
			title = fmt.Sprintf("%s (%ds)", currentStep, sec)
			stepMD = FormatStepDetailMarkdownWithOptions(lastDetail, sec, true, debug, outcome)
		case len(visibleDetails) > 0:
			lastDetail := visibleDetails[len(visibleDetails)-1]
			var thought string
			var lastDiagAct *ActivityItem
			for i := len(deltaActs) - 1; i >= 0; i-- {
				t := deltaActs[i].Tool
				if t == "kanban_heartbeat" {
					args := parseActivityArgMap(deltaActs[i].Input)
					if note := firstNonEmptyArgString(args, "note", "status", "message"); note != "" && !strings.HasPrefix(note, "<string,") {
						lastDiagAct = &deltaActs[i]
						break
					}
				}
				if !strings.HasPrefix(t, "kanban_") && t != "tool_search" && t != "tool_describe" {
					lastDiagAct = &deltaActs[i]
					break
				}
			}
			if lastDiagAct == nil && len(deltaActs) > 0 && totalCards == 0 {
				lastDiagAct = &deltaActs[len(deltaActs)-1]
			}
			switch {
			case lastDiagAct != nil:
				actStep := strings.TrimSuffix(describeActivityWaitStep(*lastDiagAct, activeAgent), ".")
				if totalCards > 0 && doneCards < totalCards {
					currentStep = fmt.Sprintf("%s (%d/%d cards done)", actStep, doneCards, totalCards)
				} else {
					currentStep = actStep
				}
				if latestNote != "" {
					thought = latestNote
				} else if totalCards > 0 && doneCards < totalCards {
					thought = fmt.Sprintf("Executed %d new tool %s across %d running specialist card(s) (%d/%d completed).", len(deltaActs), pluralizeWord(len(deltaActs), "call", "calls"), runningCards, doneCards, totalCards)
				} else {
					thought = fmt.Sprintf("Executed %d new tool %s during active step.", len(deltaActs), pluralizeWord(len(deltaActs), "call", "calls"))
				}
			case totalCards > 0 && doneCards < totalCards:
				var activeClusters []string
				for _, c := range cards {
					if c.Status == "running" || c.Status == "pending" {
						name := firstNonEmptyStr(c.Cluster, c.Title, c.ID)
						if name != "" && !strings.HasPrefix(strings.ToLower(name), "fleet-wide") && len(activeClusters) < 3 {
							activeClusters = append(activeClusters, name)
						}
					}
				}
				if len(activeClusters) > 0 {
					currentStep = fmt.Sprintf("Waiting for specialist check on %s (%d/%d cards done)", strings.Join(activeClusters, ", "), doneCards, totalCards)
				} else {
					currentStep = fmt.Sprintf("Specialist checks in progress (%d/%d completed, %d running)", doneCards, totalCards, runningCards)
				}
				if latestNote != "" {
					thought = latestNote
				} else {
					thought = fmt.Sprintf("Waiting for %d specialist card(s) to finish diagnostics (%d/%d completed).", runningCards, doneCards, totalCards)
				}
			default:
				currentStep = fmt.Sprintf("Waiting for %s to complete active step", activeAgent)
				if latestNote != "" {
					thought = latestNote
				} else {
					thought = fmt.Sprintf("Still executing: %s", strings.TrimSuffix(lastDetail.Title, "."))
				}
			}
			title = fmt.Sprintf("%s (%ds)", currentStep, sec)
			waitDetail := StepDetail{
				Title:      currentStep,
				Agent:      activeAgent,
				TaskID:     activeTaskID,
				Thought:    thought,
				Activities: deltaActs,
			}
			stepMD = FormatStepDetailMarkdownWithOptions(waitDetail, sec, false, debug, outcome)
		default:
			currentStep = fmt.Sprintf("Starting task on %s", activeAgent)
			title = fmt.Sprintf("%s (%ds)", currentStep, sec)
			stepMD = FormatStepDetailMarkdownWithOptions(StepDetail{
				Title:      currentStep,
				Agent:      activeAgent,
				TaskID:     activeTaskID,
				Activities: deltaActs,
			}, sec, false, debug, outcome)
		}

		nextSummaryHint := currentStep
		if len(allDetails) > reported {
			nextSummaryHint = strings.TrimSuffix(allDetails[reported].Title, ".")
		}

		var llm strings.Builder
		fmt.Fprintf(&llm, "%s Task `%s` is actively running on `%s` (%ds elapsed, %d %s recorded).\n",
			streamingStepContinuePrefix, outcome.PrimaryTask.TaskID, activeAgent, sec, len(visibleDetails), pluralizeStep(len(visibleDetails)))
		if len(deltaDetails) > 0 {
			fmt.Fprintf(&llm, "Latest step: %s\n", deltaDetails[len(deltaDetails)-1].Title)
		} else if len(deltaActs) > 0 {
			fmt.Fprintf(&llm, "Latest step: %s (%d new tool %s recorded)\n", currentStep, len(deltaActs), pluralizeWord(len(deltaActs), "call", "calls"))
		} else {
			llm.WriteString("No new step since the last poll: the remote agent is still inside a long-running step (remote tool calls, specialist fan-outs, and web lookups routinely take several minutes). This is normal and is NOT a stall.\n")
		}
		if done, total := countCardProgress(outcome.CardTasks); total > 0 {
			fmt.Fprintf(&llm, "Specialist cards: %d/%d completed.\n", done, total)
		} else if totalCards > 0 {
			fmt.Fprintf(&llm, "Specialist cards: %d/%d completed.\n", doneCards, totalCards)
		}
		fmt.Fprintf(&llm, "\nNEXT ACTION REQUIRED: Immediately call `mcp_kube-agents_task_control` with `action: \"get\"`, `task_id: %q`, and `toolSummary: %q` to stream the next step. Do NOT output any assistant text before calling `task_control` so that all progress steps stay in a single collapsible list. Do NOT call `list`, `steer`, `stop`, `poll_events`, `manage_session`, or read any files while this loop is active unless the user explicitly asks; just keep polling `get` until a final answer (no STREAMING_STEP_CONTINUE prefix) is returned.\n",
			outcome.PrimaryTask.TaskID, truncateText(nextSummaryHint, 48))

		return title, stepMD, strings.TrimSpace(llm.String())
	}

	// Settled / completed task:
	answerOnlyMD := formatCleanTurnAnswerOnlyMarkdown(outcome)
	llmOut := answerOnlyMD
	if debug {
		llmOut = formatCompactDebugLLMResult(outcome, answerOnlyMD)
	}
	if prev > 0 {
		if len(deltaDetails) >= 1 {
			lastDetail := deltaDetails[len(deltaDetails)-1]
			lastStep := strings.TrimSuffix(lastDetail.Title, ".")
			title = fmt.Sprintf("%s (%ds)", lastStep, sec)
			userMD := FormatStepDetailsBatchMarkdownWithOptions(deltaDetails, sec, true, debug, outcome)
			if debug {
				userMD = fullMD
			}
			return title, userMD, llmOut
		}
		title = fmt.Sprintf("Completed in %ds (%d %s)", sec, len(allDetails), pluralizeStep(len(allDetails)))
		userMD := fmt.Sprintf("- **Agent**: `%s` (task `%s`)\n- **Status**: ✅ Completed in %ds (%d %s)",
			activeAgent, activeTaskID, sec, len(allDetails), pluralizeStep(len(allDetails)))
		if debug {
			userMD = fullMD
		}
		return title, userMD, llmOut
	}

	if len(allDetails) >= 1 {
		lastDetail := allDetails[len(allDetails)-1]
		lastStep := strings.TrimSuffix(lastDetail.Title, ".")
		title = fmt.Sprintf("%s (%ds)", lastStep, sec)
		userMD := FormatStepDetailsBatchMarkdownWithOptions(allDetails, sec, true, debug, outcome)
		if debug {
			if len(allDetails) > 1 {
				title = fmt.Sprintf("Completed in %ds (%d %s)", sec, len(allDetails), pluralizeStep(len(allDetails)))
			}
			userMD = fullMD
		}
		return title, userMD, llmOut
	}
	title = fmt.Sprintf("Completed in %ds", sec)
	userMD := fmt.Sprintf("- **Agent**: `%s` (task `%s`)\n- **Status**: ✅ Task completed in %ds", activeAgent, activeTaskID, sec)
	if debug {
		userMD = fullMD
	}
	return title, userMD, llmOut
}

func formatCompactDebugLLMResult(outcome *TurnOutcome, answerOnlyMD string) string {
	if outcome == nil || outcome.PrimaryTask == nil {
		return answerOnlyMD
	}
	rec := outcome.PrimaryTask
	sec := computeOutcomeElapsedSeconds(rec, outcome.ChildTasks, outcome.WakeTask)
	cards, doneCards, runningCards, totalCards, _ := extractKanbanCardsFromTask(rec)
	if totalCards == 0 {
		for _, child := range outcome.ChildTasks {
			cCards, cDone, cRunning, cTotal, _ := extractKanbanCardsFromTask(child)
			if cTotal > 0 {
				cards = append(cards, cCards...)
				doneCards += cDone
				runningCards += cRunning
				totalCards += cTotal
			}
		}
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "### Debug Telemetry Summary (`%s`)\n", rec.TaskID)
	fmt.Fprintf(&sb, "- **Task / Context**: `%s` / `%s` (`%s`, `%s`, `%ds` elapsed)\n",
		rec.TaskID, firstNonEmptyStr(rec.ContextID, "—"), firstNonEmptyStr(rec.Addressee, "platform"), rec.State, sec)
	fmt.Fprintf(&sb, "- **Envelopes & Tool Calls**: `%d` envelopes (%s), `%d` tool calls\n",
		rec.EnvelopeCount, firstNonEmptyStr(formatEnvelopeKindBreakdown(rec.EnvelopeCountsByKind), "—"), len(rec.Activities))
	if len(outcome.ChildTasks) > 0 || len(rec.Delegates) > 0 {
		var hops []string
		hops = append(hops, fmt.Sprintf("`%s` (`%s`)", firstNonEmptyStr(rec.Addressee, "session"), rec.TaskID))
		if len(outcome.ChildTasks) > 0 {
			for _, child := range outcome.ChildTasks {
				if child == nil {
					continue
				}
				hops = append(hops, fmt.Sprintf("`%s` (`%s`, `%d` envelopes, `%d` tool calls)",
					firstNonEmptyStr(child.Addressee, "platform"), child.TaskID, child.EnvelopeCount, len(child.Activities)))
			}
		} else {
			for _, d := range rec.Delegates {
				if d.ChildTaskID != "" {
					hops = append(hops, fmt.Sprintf("`%s` (`%s`)", d.Addressee, d.ChildTaskID))
				} else {
					hops = append(hops, fmt.Sprintf("`%s`", d.Addressee))
				}
			}
		}
		if outcome.WakeTask != nil {
			hops = append(hops, fmt.Sprintf("`%s` wake (`%s`, `%d` envelopes)",
				firstNonEmptyStr(outcome.WakeTask.Addressee, rec.Addressee, "session"), outcome.WakeTask.TaskID, outcome.WakeTask.EnvelopeCount))
		}
		fmt.Fprintf(&sb, "- **Delegation Chain**: %s\n", strings.Join(hops, " → "))
	}
	if len(rec.SteerTimestamps) > 0 {
		var steerTimes []string
		for _, ts := range rec.SteerTimestamps {
			steerTimes = append(steerTimes, ts.UTC().Format("15:04:05 UTC"))
		}
		fmt.Fprintf(&sb, "- **Steer / Follow-Up Timeline**: `%d` steer(s) at `%s`\n", len(rec.SteerTimestamps), strings.Join(steerTimes, "`, `"))
	}
	if totalCards > 0 {
		fmt.Fprintf(&sb, "- **Specialist Kanban Cards**: `%d/%d` completed (`%d` running)\n", doneCards, totalCards, runningCards)
		for _, c := range cards {
			fmt.Fprintf(&sb, "  - `%s` (%s): status=`%s`, verdict=`%s` — %s\n",
				c.ID, firstNonEmptyStr(c.Cluster, c.Title, "—"), c.Status, firstNonEmptyStr(c.Verdict, c.Status), truncateText(c.Summary, 140))
		}
	}
	sb.WriteString("\n### Final Answer\n\n")
	sb.WriteString(answerOnlyMD)
	return strings.TrimSpace(sb.String()) + "\n"
}

const maxDebugStepActivitiesShown = 12

// FormatStepDetailsBatchMarkdown renders the expandable markdown body for one or more StepDetails.
func FormatStepDetailsBatchMarkdown(details []StepDetail, elapsedSec int, completed bool) string {
	return FormatStepDetailsBatchMarkdownWithOptions(details, elapsedSec, completed, false, nil)
}

// FormatStepDetailsBatchMarkdownWithOptions renders the expandable markdown body for one or more StepDetails.
func FormatStepDetailsBatchMarkdownWithOptions(details []StepDetail, elapsedSec int, completed bool, debug bool, outcome *TurnOutcome) string {
	if len(details) == 0 {
		return ""
	}
	if len(details) == 1 {
		return FormatStepDetailMarkdownWithOptions(details[0], elapsedSec, completed, debug, outcome)
	}
	var parts []string
	for _, d := range details {
		header := fmt.Sprintf("**%s**", strings.TrimSuffix(d.Title, "."))
		parts = append(parts, header+"\n"+FormatStepDetailMarkdownWithOptions(d, elapsedSec, completed, debug, outcome))
	}
	return strings.Join(parts, "\n\n")
}

// FormatStepDetailMarkdown renders the rich expandable markdown body for a single step row
// inside Jetski's ToolGroupCollapsible, showing Agent & Task ID, Thought / Reasoning (or
// Delegation Request), and Tool Activity (tool name, argument summary, status, and duration).
func FormatStepDetailMarkdown(detail StepDetail, elapsedSec int, completed bool) string {
	return FormatStepDetailMarkdownWithOptions(detail, elapsedSec, completed, false, nil)
}

// FormatStepDetailMarkdownWithOptions renders the expandable markdown body for a single step row,
// adding bus envelope & worker/session telemetry, unshaped arguments, and tool result previews when debug is true.
func FormatStepDetailMarkdownWithOptions(detail StepDetail, elapsedSec int, completed bool, debug bool, outcome *TurnOutcome) string {
	var sb strings.Builder
	stepTitleClean := strings.TrimSuffix(strings.TrimSpace(detail.Title), ".")

	if detail.Agent != "" && detail.TaskID != "" {
		if debug && outcome != nil && outcome.PrimaryTask != nil && outcome.PrimaryTask.ContextID != "" {
			sessionID := ""
			for _, a := range detail.Activities {
				if a.SessionID != "" {
					sessionID = a.SessionID
					break
				}
			}
			if sessionID != "" {
				fmt.Fprintf(&sb, "- **Agent**: `%s` (task `%s`, context `%s`, session `%s`)\n",
					detail.Agent, detail.TaskID, outcome.PrimaryTask.ContextID, sessionID)
			} else {
				fmt.Fprintf(&sb, "- **Agent**: `%s` (task `%s`, context `%s`)\n",
					detail.Agent, detail.TaskID, outcome.PrimaryTask.ContextID)
			}
		} else {
			fmt.Fprintf(&sb, "- **Agent**: `%s` (task `%s`)\n", detail.Agent, detail.TaskID)
		}
	} else if detail.Agent != "" {
		fmt.Fprintf(&sb, "- **Agent**: `%s`\n", detail.Agent)
	}

	if debug && outcome != nil && outcome.PrimaryTask != nil {
		rec := outcome.PrimaryTask
		if rec.EnvelopeCount > 0 {
			breakdown := formatEnvelopeKindBreakdown(rec.EnvelopeCountsByKind)
			deltaActs := len(collectDeltaActivities(outcome))
			if breakdown != "" {
				fmt.Fprintf(&sb, "- **Bus Envelopes**: `%d` folded (%s) | `+%d` new tool %s\n",
					rec.EnvelopeCount, breakdown, deltaActs, pluralizeWord(deltaActs, "call", "calls"))
			} else {
				fmt.Fprintf(&sb, "- **Bus Envelopes**: `%d` folded | `+%d` new tool %s\n",
					rec.EnvelopeCount, deltaActs, pluralizeWord(deltaActs, "call", "calls"))
			}
		}
	}

	if detail.DelegatedTo != "" {
		if detail.ChildTaskID != "" {
			fmt.Fprintf(&sb, "- **Delegated To**: `%s` (child task `%s`)\n", detail.DelegatedTo, detail.ChildTaskID)
		} else {
			fmt.Fprintf(&sb, "- **Delegated To**: `%s`\n", detail.DelegatedTo)
		}
	}

	if detail.Thought != "" {
		thoughtSingle := strings.Join(strings.Fields(detail.Thought), " ")
		fmt.Fprintf(&sb, "- **Thought**: *%s*\n", thoughtSingle)
	}
	if detail.DelegatePrompt != "" {
		promptSingle := strings.Join(strings.Fields(detail.DelegatePrompt), " ")
		fmt.Fprintf(&sb, "- **Delegation Request**: *%s*\n", truncateText(promptSingle, 280))
	}

	if len(detail.Activities) > 0 {
		sb.WriteString("- **Tool Activity**:\n")
		for _, line := range formatStepActivitiesListWithOptions(detail.Activities, stepTitleClean, debug) {
			fmt.Fprintf(&sb, "  - %s\n", line)
		}
	} else if detail.DelegatedTo == "" && detail.Thought == "" {
		if strings.EqualFold(stepTitleClean, strings.TrimSuffix(synthFinalStepTitle, ".")) {
			fmt.Fprintf(&sb, "- **Action**: *Synthesizing tool outputs and cluster telemetry into final response* _(%ds elapsed)_\n", elapsedSec)
		} else if completed {
			fmt.Fprintf(&sb, "- **Action**: *%s* _(%ds elapsed)_\n", stepTitleClean, elapsedSec)
		} else {
			fmt.Fprintf(&sb, "- **Action**: *%s...* _(%ds elapsed)_\n", stepTitleClean, elapsedSec)
		}
	}

	return strings.TrimSpace(sb.String())
}

func formatEnvelopeKindBreakdown(counts map[string]int) string {
	if len(counts) == 0 {
		return ""
	}
	keys := make([]string, 0, len(counts))
	for k, v := range counts {
		if v > 0 {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("`%s=%d`", k, counts[k]))
	}
	return strings.Join(parts, ", ")
}

func formatStepActivitiesList(activities []ActivityItem, stepTitleClean string) []string {
	return formatStepActivitiesListWithOptions(activities, stepTitleClean, false)
}

func formatStepActivitiesListWithOptions(activities []ActivityItem, stepTitleClean string, debug bool) []string {
	var lines []string
	maxShown := maxStepActivitiesShown
	if debug {
		maxShown = maxDebugStepActivitiesShown
	}
	limit := len(activities)
	if limit > maxShown {
		limit = maxShown - 1
	}
	for i := 0; i < limit; i++ {
		lines = append(lines, formatSingleActivityLineWithOptions(activities[i], stepTitleClean, debug))
	}
	if len(activities) > limit {
		remaining := len(activities) - limit
		var remMs int64
		for i := limit; i < len(activities); i++ {
			remMs += activities[i].DurationMs
		}
		if remMs > 0 {
			lines = append(lines, fmt.Sprintf("*(+ %d more tool %s, %s total)*", remaining, pluralizeWord(remaining, "call", "calls"), formatDurationMs(remMs)))
		} else {
			lines = append(lines, fmt.Sprintf("*(+ %d more tool %s)*", remaining, pluralizeWord(remaining, "call", "calls")))
		}
	}
	return lines
}

func formatSingleActivityLine(act ActivityItem, stepTitleClean string) string {
	return formatSingleActivityLineWithOptions(act, stepTitleClean, false)
}

func formatSingleActivityLineWithOptions(act ActivityItem, stepTitleClean string, debug bool) string {
	maxValChars := 64
	if debug {
		maxValChars = 240
	}
	toolDisplay := fmt.Sprintf("**`%s`**", act.Tool)
	argSummary := summarizeActivityInputWithLimit(act.Tool, act.Input, maxValChars)

	if act.Tool == "tool_call" {
		nestedDisplay, nestedArgs := summarizeBatchWrapperCallWithLimit(act.Input, maxValChars)
		if nestedDisplay != "" {
			toolDisplay = nestedDisplay
			argSummary = nestedArgs
		}
	}

	statusIcon := "✅"
	statusWord := firstNonEmptyStr(act.Status, "completed")
	switch statusWord {
	case "ok", "completed":
		statusIcon = "✅"
		statusWord = "completed"
	case "error", "failed":
		statusIcon = "❌"
		if act.ErrorType != "" && act.ErrorType != statusWord {
			statusWord = fmt.Sprintf("error: %s", act.ErrorType)
		}
	case "interrupted":
		statusIcon = "⚠️"
	default:
		statusIcon = "🔄"
	}

	timingPart := fmt.Sprintf("%s %s", statusIcon, statusWord)
	if act.DurationMs > 0 {
		timingPart = fmt.Sprintf("%s %s in %s", statusIcon, statusWord, formatDurationMs(act.DurationMs))
	}

	prefixBadge := "🛠️"
	if debug {
		var metaParts []string
		if act.Index > 0 {
			metaParts = append(metaParts, fmt.Sprintf("`#%d`", act.Index))
		}
		if tStr := formatActivityAtTime(act.At); tStr != "" {
			metaParts = append(metaParts, fmt.Sprintf("`%s`", tStr))
		}
		worker := strings.TrimSpace(act.Worker)
		if worker == "" {
			worker = "platform"
		}
		metaParts = append(metaParts, fmt.Sprintf("`[%s]`", worker))
		prefixBadge = "🛠️ " + strings.Join(metaParts, " ")
	} else if act.Worker != "" && act.Worker != "platform" {
		prefixBadge = fmt.Sprintf("🛠️ `[%s]`", act.Worker)
	}

	resultSuffix := ""
	if debug {
		if act.ErrorMessage != "" {
			resultSuffix = fmt.Sprintf(" → ❌ `%s`", truncateText(strings.ReplaceAll(act.ErrorMessage, "\n", " "), 180))
		} else if act.ResultPreview != "" {
			resultSuffix = fmt.Sprintf(" → `%s`", truncateText(strings.ReplaceAll(act.ResultPreview, "\n", " "), 180))
		}
	}

	desc := strings.TrimSuffix(toolNameToStepTitle(act.Tool), ".")
	if argSummary != "" {
		return fmt.Sprintf("%s %s — %s *(%s)*%s", prefixBadge, toolDisplay, argSummary, timingPart, resultSuffix)
	}
	if desc != "" && !strings.EqualFold(desc, "Running "+act.Tool) {
		return fmt.Sprintf("%s %s — %s *(%s)*%s", prefixBadge, toolDisplay, desc, timingPart, resultSuffix)
	}
	if stepTitleClean != "" {
		return fmt.Sprintf("%s %s — %s *(%s)*%s", prefixBadge, toolDisplay, stepTitleClean, timingPart, resultSuffix)
	}
	return fmt.Sprintf("%s %s *(%s)*%s", prefixBadge, toolDisplay, timingPart, resultSuffix)
}

func summarizeBatchWrapperCall(rawInput string) (toolDisplay string, argSummary string) {
	return summarizeBatchWrapperCallWithLimit(rawInput, 64)
}

func summarizeBatchWrapperCallWithLimit(rawInput string, maxValChars int) (toolDisplay string, argSummary string) {
	var wrapper struct {
		Calls []struct {
			Name      string         `json:"name"`
			Arguments map[string]any `json:"arguments"`
		} `json:"calls"`
	}
	if json.Unmarshal([]byte(rawInput), &wrapper) != nil || len(wrapper.Calls) == 0 {
		return "", ""
	}
	counts := make(map[string]int)
	var order []string
	var firstArgs map[string]any
	for _, c := range wrapper.Calls {
		name := strings.TrimSpace(c.Name)
		if name == "" {
			continue
		}
		if counts[name] == 0 {
			order = append(order, name)
		}
		counts[name]++
		if firstArgs == nil && len(c.Arguments) > 0 {
			firstArgs = c.Arguments
		}
	}
	if len(order) == 0 {
		return "", ""
	}
	var parts []string
	for _, name := range order {
		if counts[name] > 1 {
			parts = append(parts, fmt.Sprintf("**`%s`** (×%d)", name, counts[name]))
		} else {
			parts = append(parts, fmt.Sprintf("**`%s`**", name))
		}
	}
	toolDisplay = strings.Join(parts, ", ")
	if firstArgs != nil {
		argSummary = formatArgMapSummaryWithLimit(firstArgs, maxValChars)
	}
	return toolDisplay, argSummary
}

func summarizeActivityInput(tool string, rawInput string) string {
	return summarizeActivityInputWithLimit(tool, rawInput, 64)
}

func summarizeActivityInputWithLimit(tool string, rawInput string, maxValChars int) string {
	trimmed := strings.TrimSpace(rawInput)
	if trimmed == "" || trimmed == "{}" || trimmed == "null" {
		return ""
	}
	var m map[string]any
	if json.Unmarshal([]byte(trimmed), &m) == nil {
		if len(m) == 0 {
			return ""
		}
		return formatArgMapSummaryWithLimit(m, maxValChars)
	}
	if m := shapedStringPlaceholderRe.FindStringSubmatch(trimmed); len(m) == 2 {
		return fmt.Sprintf("input (%s chars)", m[1])
	}
	return fmt.Sprintf("`%s`", truncateText(strings.ReplaceAll(trimmed, "\n", " "), maxValChars))
}

func formatArgMapSummary(m map[string]any) string {
	return formatArgMapSummaryWithLimit(m, 64)
}

func formatArgMapSummaryWithLimit(m map[string]any, maxValChars int) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		if strings.HasPrefix(k, "<key ") {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		val := m[k]
		switch v := val.(type) {
		case string:
			if sm := shapedStringPlaceholderRe.FindStringSubmatch(v); len(sm) == 2 {
				parts = append(parts, fmt.Sprintf("`%s` (%s chars)", k, sm[1]))
			} else if v == "[redacted]" {
				parts = append(parts, fmt.Sprintf("`%s`: `[redacted]`", k))
			} else {
				parts = append(parts, fmt.Sprintf("`%s`: `%s`", k, truncateText(strings.ReplaceAll(v, "\n", " "), maxValChars)))
			}
		case float64, int, int64, json.Number, bool:
			parts = append(parts, fmt.Sprintf("`%s`: `%v`", k, v))
		default:
			if b, err := json.Marshal(v); err == nil && len(b) > 0 && string(b) != "null" {
				parts = append(parts, fmt.Sprintf("`%s`: `%s`", k, truncateText(string(b), maxValChars)))
			} else {
				parts = append(parts, fmt.Sprintf("`%s`", k))
			}
		}
	}
	return strings.Join(parts, ", ")
}

func formatDurationMs(ms int64) string {
	if ms < 1000 {
		return fmt.Sprintf("%dms", ms)
	}
	return fmt.Sprintf("%.1fs", float64(ms)/1000.0)
}

func pluralizeWord(n int, singular, plural string) string {
	if n == 1 {
		return singular
	}
	return plural
}

func pluralizeStep(n int) string {
	if n == 1 {
		return "step"
	}
	return "steps"
}

func formatCleanTurnOutcomeMarkdown(outcome *TurnOutcome) string {
	var sb strings.Builder

	// Pure control command (e.g. /session or /session off) with no task:
	if outcome.PrimaryTask == nil {
		for _, n := range outcome.ConsoleNotices {
			if txt := strings.TrimSpace(n.Text); txt != "" {
				fmt.Fprintf(&sb, "%s\n", txt)
			}
		}
		return strings.TrimSpace(sb.String())
	}

	thoughts := CollectOutcomeSteps(outcome)
	summaryLabel := formatThoughtSummaryLabel(outcome.PrimaryTask, outcome.ChildTasks, outcome.WakeTask, len(thoughts))
	if len(thoughts) > 0 {
		sb.WriteString("<details>\n")
		fmt.Fprintf(&sb, "<summary>%s</summary>\n\n", summaryLabel)
		for _, line := range thoughts {
			fmt.Fprintf(&sb, "- ✅ %s\n", line)
		}
		sb.WriteString("\n</details>\n\n")
	}

	sb.WriteString(formatCleanTurnAnswerOnlyMarkdown(outcome))
	return strings.TrimSpace(sb.String()) + "\n"
}

// formatCleanTurnAnswerOnlyMarkdown renders ONLY the final answer and any PR links (without
// repeating the <details><summary>Thought...</summary></details> block) for Jetski custom tool
// envelopes where all steps are already displayed in the collapsible tool group above.
func formatCleanTurnAnswerOnlyMarkdown(outcome *TurnOutcome) string {
	if outcome == nil {
		return ""
	}
	var sb strings.Builder
	if outcome.PrimaryTask == nil {
		for _, n := range outcome.ConsoleNotices {
			if txt := strings.TrimSpace(n.Text); txt != "" {
				fmt.Fprintf(&sb, "%s\n", txt)
			}
		}
		return strings.TrimSpace(sb.String())
	}

	resultBody := renderTaskAnswerBody(outcome.WakeTask)
	if resultBody == "" && len(outcome.ChildTasks) > 0 {
		var parts []string
		if pBody := renderTaskAnswerBody(outcome.PrimaryTask); pBody != "" {
			parts = append(parts, pBody)
		}
		for _, child := range outcome.ChildTasks {
			if cBody := renderTaskAnswerBody(child); cBody != "" {
				parts = append(parts, cBody)
			}
		}
		resultBody = strings.Join(parts, "\n\n")
	}
	if resultBody == "" {
		resultBody = renderTaskAnswerBody(outcome.PrimaryTask)
	}
	if resultBody != "" {
		sb.WriteString(resultBody)
		if !strings.HasSuffix(resultBody, "\n") {
			sb.WriteString("\n")
		}
	}

	allPRs := collectOutcomePRs(outcome)
	var missingPRs []string
	for _, pr := range allPRs {
		if !strings.Contains(resultBody, pr) {
			missingPRs = append(missingPRs, pr)
		}
	}
	if len(missingPRs) > 0 {
		sb.WriteString("\n**Pull Request**: " + strings.Join(missingPRs, ", ") + "\n")
	}

	if outcome.TimedOut && outcome.PrimaryTask != nil {
		sb.WriteString("\n> [!NOTE]\n")
		fmt.Fprintf(&sb, "> Task `%s` is still in flight (`%s`). Wait for the automatic background push wakeup or call `task_control` (`action: \"get\"`, `task_id: %q`).\n",
			outcome.PrimaryTask.TaskID, outcome.PrimaryTask.State, outcome.PrimaryTask.TaskID)
	}

	return strings.TrimSpace(sb.String()) + "\n"
}

func isSilentResultText(s string) bool {
	t := strings.Trim(strings.TrimSpace(s), "`*_ ")
	return strings.EqualFold(t, "[SILENT]")
}

func hasStructuredChatContent(cm *chatmsg.Message) bool {
	if cm == nil {
		return false
	}
	return strings.TrimSpace(cm.Detail) != "" ||
		len(cm.Rows) > 0 ||
		cm.Plan != nil ||
		cm.Fold != nil ||
		len(cm.Choices) > 0 ||
		len(cm.Links) > 0 ||
		(cm.Tone != "" && cm.Tone != "neutral")
}

func renderTaskAnswerBody(rec *TaskRecord) string {
	if rec == nil {
		return ""
	}
	var sb strings.Builder
	plain := strings.TrimSpace(rec.ResultText)
	if isSilentResultText(plain) {
		// The chat agents' "[SILENT]" sentinel means "someone else already posted the answer";
		// on a console conversation nobody did, so show the evidence instead of the token.
		plain = ""
		for i := len(rec.IntermediateTurns) - 1; i >= 0; i-- {
			if t := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(rec.IntermediateTurns[i]), cardResultsRawHeader)); t != "" && !isSilentResultText(t) {
				plain = t
				break
			}
		}
		if plain == "" {
			plain = silentResultFallback
		}
	}
	if rec.ResultChat != nil || plain != "" {
		if rec.ResultChat != nil && (hasStructuredChatContent(rec.ResultChat) || plain == "") {
			sb.WriteString(FormatChatMessageMarkdown(rec.ResultChat))
			if plain != "" && rec.ResultChat.Detail == "" && len(rec.ResultChat.Rows) == 0 {
				fmt.Fprintf(&sb, "\n%s\n", plain)
			}
		} else {
			fmt.Fprintf(&sb, "%s\n", plain)
		}
	} else if !rec.Terminal && (rec.ProgressChat != nil || strings.TrimSpace(rec.ProgressText) != "") {
		if rec.ProgressChat != nil {
			sb.WriteString(FormatChatMessageMarkdown(rec.ProgressChat))
		} else {
			fmt.Fprintf(&sb, "%s\n", strings.TrimSpace(rec.ProgressText))
		}
	} else if rec.StatusText != "" {
		fmt.Fprintf(&sb, "%s\n", strings.TrimSpace(rec.StatusText))
	}
	return strings.TrimSpace(sb.String())
}

func computeOutcomeElapsedSeconds(primary *TaskRecord, children []*TaskRecord, wake *TaskRecord) int {
	if primary == nil {
		return 0
	}
	start := primary.CreatedAt
	end := primary.UpdatedAt
	anyRunning := !primary.Terminal
	for _, c := range children {
		if c != nil {
			if c.UpdatedAt.After(end) {
				end = c.UpdatedAt
			}
			if !c.Terminal {
				anyRunning = true
			}
		}
	}
	if wake != nil {
		if wake.UpdatedAt.After(end) {
			end = wake.UpdatedAt
		}
		if !wake.Terminal {
			anyRunning = true
		}
	}
	if anyRunning {
		now := time.Now().UTC()
		if now.After(end) {
			end = now
		}
	}
	if !start.IsZero() && end.After(start) {
		sec := int(end.Sub(start).Round(time.Second).Seconds())
		if sec >= 1 {
			return sec
		}
	}
	var totalMs int64
	for _, a := range primary.Activities {
		totalMs += a.DurationMs
	}
	for _, c := range children {
		if c != nil {
			for _, a := range c.Activities {
				totalMs += a.DurationMs
			}
		}
	}
	if totalMs >= 1000 {
		return int((time.Duration(totalMs) * time.Millisecond).Round(time.Second).Seconds())
	}
	return 0
}

func formatThoughtSummaryLabel(primary *TaskRecord, children []*TaskRecord, wake *TaskRecord, stepCount int) string {
	sec := computeOutcomeElapsedSeconds(primary, children, wake)
	if sec >= 1 && stepCount > 0 {
		return fmt.Sprintf("Thought for %ds (%d %s)", sec, stepCount, pluralizeStep(stepCount))
	}
	if sec >= 1 {
		return fmt.Sprintf("Thought for %ds", sec)
	}
	if stepCount > 0 {
		return fmt.Sprintf("Thought (%d %s)", stepCount, pluralizeStep(stepCount))
	}
	return "Thought"
}

func cleanAndDedupeThinking(raw string) []string {
	steps := parseAndCleanThinkingSteps(raw)
	out := make([]string, 0, len(steps))
	for _, s := range steps {
		out = append(out, s.Title)
	}
	return out
}

var (
	fleetWideParenSuffixRe = regexp.MustCompile(`(?i)\s*\(fleet-wide [^)]*(?:\([^.)]*\))?[^.)]*\)?\.?$`)
	trailingClusterParenRe = regexp.MustCompile(`\s*\(([a-z0-9][a-z0-9_-]{2,47})\)\.?$`)
)

func parseAndCleanThinkingSteps(raw string) []StepDetail {
	rawLines := strings.Split(raw, "\n")
	consolidateClusterDiagnostics := len(rawLines) > 40
	dedupeNonAdjacent := len(rawLines) > 8
	var out []StepDetail
	for _, line := range rawLines {
		title, thought := normalizeStepTitleAndThought(line)
		if title == "" {
			continue
		}
		if consolidateClusterDiagnostics {
			lowerTitle := strings.ToLower(title)
			if !strings.HasPrefix(lowerTitle, "specialist check ") &&
				!strings.HasPrefix(lowerTitle, "delegating specialist task") &&
				!strings.HasPrefix(lowerTitle, "inspecting gke cluster ") {
				if m := trailingClusterParenRe.FindStringSubmatch(title); len(m) == 2 {
					clusterTag := m[1]
					baseTitle := strings.TrimSpace(title[:len(title)-len(m[0])])
					if baseTitle != "" {
						title = strings.TrimSuffix(baseTitle, ".") + "."
						if thought != "" && !strings.Contains(thought, clusterTag) {
							thought = "[" + clusterTag + "] " + thought
						}
					}
				}
			}
		}
		normKey := strings.ToLower(strings.TrimSuffix(title, "."))
		if len(out) > 0 && strings.EqualFold(strings.TrimSuffix(out[len(out)-1].Title, "."), normKey) {
			if thought != "" {
				out[len(out)-1].Thought = mergeThoughts(out[len(out)-1].Thought, thought)
			}
			continue
		}
		if dedupeNonAdjacent {
			isSynth := strings.EqualFold(normKey, strings.TrimSuffix(synthFinalStepTitle, "."))
			foundIdx := -1
			for i := range out {
				if strings.EqualFold(strings.TrimSuffix(out[i].Title, "."), normKey) {
					foundIdx = i
					break
				}
			}
			if foundIdx >= 0 {
				if isSynth {
					mergedThought := mergeThoughts(out[foundIdx].Thought, thought)
					out = append(out[:foundIdx], out[foundIdx+1:]...)
					out = append(out, StepDetail{
						Title:   title,
						Thought: mergedThought,
					})
				} else if thought != "" {
					out[foundIdx].Thought = mergeThoughts(out[foundIdx].Thought, thought)
				}
				continue
			}
		}
		out = append(out, StepDetail{
			Title:   title,
			Thought: thought,
		})
	}
	return out
}

func normalizeStepTitleAndThought(rawLine string) (title string, thought string) {
	rawLine = strings.TrimSpace(rawLine)
	if rawLine == "" {
		return "", ""
	}
	if strings.Contains(rawLine, "\t") {
		parts := strings.SplitN(rawLine, "\t", 2)
		title = cleanSingleThinkingLine(parts[0])
		thought = strings.TrimSpace(parts[1])
		if strings.EqualFold(strings.TrimSuffix(title, "."), strings.TrimSuffix(thought, ".")) {
			thought = ""
		}
		return title, thought
	}

	cleaned := cleanSingleThinkingLine(rawLine)
	if cleaned == "" {
		return "", ""
	}

	if m := kanbanProgressDoneRe.FindStringSubmatch(cleaned); len(m) == 2 {
		return fmt.Sprintf("Waiting for specialist agent tasks (%s completed).", m[1]), cleaned
	}
	if kanbanStillRunningRe.MatchString(cleaned) {
		return "Waiting for specialist agent tasks to complete.", cleaned
	}
	if preflightDiagRe.MatchString(cleaned) {
		return "Completing preflight checks and starting cluster diagnostics.", cleaned
	}
	if nodePoolInspectRe.MatchString(cleaned) {
		return "Inspecting Kubernetes pods, nodes, and cluster events.", cleaned
	}

	return cleaned, ""
}

func cleanSingleThinkingLine(line string) string {
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, ">")
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, "- ")
	line = strings.TrimPrefix(line, "• ")
	line = legacyStepPrefixRe.ReplaceAllString(line, "")
	line = legacyActionPrefixRe.ReplaceAllString(line, "")
	line = legacyPlanPrefixRe.ReplaceAllString(line, "")
	line = legacyKeyBracketSufRe.ReplaceAllString(line, "")
	if fleetWideParenSuffixRe.MatchString(line) {
		hasDot := strings.HasSuffix(strings.TrimSpace(line), ".")
		line = strings.TrimSpace(fleetWideParenSuffixRe.ReplaceAllString(line, ""))
		if hasDot && line != "" && !strings.HasSuffix(line, ".") {
			line += "."
		}
	}
	if legacySynthPrefixRe.MatchString(line) {
		line = synthFinalStepTitle
	}
	switch {
	case strings.Contains(line, "running cluster/shell diagnostic"):
		line = "Running cluster and workspace diagnostics."
	case strings.Contains(line, "listing GKE clusters"):
		line = "Listing GKE clusters in the fleet."
	case strings.Contains(line, "inspecting GKE cluster details"):
		line = "Inspecting GKE cluster configuration."
	case strings.Contains(line, "querying GKE MCP"):
		line = "Querying GKE cluster state."
	case strings.Contains(line, "updating/querying Kanban board"):
		line = "Updating task status on the Kanban board."
	case strings.Contains(line, "inspecting workspace files"):
		line = "Inspecting files in the workspace."
	case line == "Running read_file.":
		line = "Reading file in the workspace."
	case line == "Running search_files." || line == "Running find_by_name." || line == "Running list_dir." || line == "Running glob.":
		line = "Inspecting files in the workspace."
	case line == "Running patch." || line == "Running edit_file." || line == "Running replace_in_file.":
		line = "Editing a file in the agent workspace."
	case line == "Running write_file." || line == "Running create_file.":
		line = "Writing a file in the agent workspace."
	case line == "Running mcp__platform_control__list_cluster_profiles.":
		line = "Listing GKE cluster profiles in the fleet."
	case strings.HasPrefix(line, "Running mcp__platform_control__"):
		line = "Querying platform control state."
	case line == "Running mcp__a2a__delegate.":
		line = "Delegating task to platform."
	case strings.HasPrefix(line, "Running mcp__a2a__"):
		line = "Communicating over the A2A bus."
	case strings.HasPrefix(line, "Running mcp__developer_knowledge__"):
		line = "Searching documentation in Developer Knowledge."
	case line == "Running web_search.":
		line = "Searching the web."
	case line == "Running web_extract." || line == "Running web_fetch." || line == "Running read_url_content.":
		line = "Reading web page content."
	case line == "Running tool_search.":
		line = "Searching available tools."
	case line == "Running tool_describe.":
		line = "Inspecting tool schema."
	case line == "Running execute_code.":
		line = "Running analysis script."
	case line == "Running skill_view.":
		line = "Loading skill instructions."
	case line == "Running kanban_create.":
		line = "Delegating task to specialist agent."
	}
	return strings.TrimSpace(line)
}

// FormatTaskRecordMarkdown renders a single TaskRecord using default clean formatting.
func FormatTaskRecordMarkdown(rec *TaskRecord, titlePrefix string) string {
	return FormatTaskRecordMarkdownWithOptions(rec, titlePrefix, false)
}

// FormatTaskRecordMarkdownWithOptions renders a single TaskRecord.
// When debug is false, thinking is rendered inside a collapsed <details> block and the Tool Activity
// Stream table is omitted. When debug is true, full task metadata and the 4 collapsible debug panels
// (Bus Envelope & Task Telemetry, Kanban Specialist Cards, Full Tool Activity Stream, and Raw LLM API Turn Log)
// are included.
func FormatTaskRecordMarkdownWithOptions(rec *TaskRecord, titlePrefix string, debug bool) string {
	if rec == nil {
		return ""
	}
	var sb strings.Builder
	if debug {
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
	}

	thoughts := CollectOutcomeSteps(&TurnOutcome{PrimaryTask: rec})
	if len(thoughts) > 0 {
		label := formatThoughtSummaryLabel(rec, nil, nil, len(thoughts))
		sb.WriteString("<details>\n")
		fmt.Fprintf(&sb, "<summary>%s</summary>\n\n", label)
		const (
			maxDebugThoughts = 15
			headThoughts     = 6
			tailThoughts     = 9
		)
		for i, line := range thoughts {
			if debug && len(thoughts) > maxDebugThoughts && i >= headThoughts && i < len(thoughts)-tailThoughts {
				if i == headThoughts {
					fmt.Fprintf(&sb, "- *… (%d intermediate steps omitted — see telemetry panels below)*\n", len(thoughts)-headThoughts-tailThoughts)
				}
				continue
			}
			fmt.Fprintf(&sb, "- ✅ %s\n", line)
		}
		sb.WriteString("\n</details>\n\n")
	}

	if debug {
		sec := computeOutcomeElapsedSeconds(rec, nil, nil)
		if sec < 1 {
			sec = 1
		}
		addressee := firstNonEmptyStr(rec.Addressee, "platform-agent")

		// Panel 1: Bus Envelope & Task Telemetry
		fmt.Fprintf(&sb, "<details open>\n<summary>📡 <strong>Bus Envelope &amp; Task Telemetry</strong> — <code>%s</code> → <code>%s</code> (<code>%d envelopes</code>, <code>%ds</code>)</summary>\n\n",
			rec.TaskID, addressee, rec.EnvelopeCount, sec)
		sb.WriteString("| Field | Value |\n| :--- | :--- |\n")
		fmt.Fprintf(&sb, "| **Task & Context ID** | `%s` / `%s` (conversation `%s`) |\n",
			rec.TaskID, firstNonEmptyStr(rec.ContextID, "—"), firstNonEmptyStr(rec.Conversation, "default"))
		fmt.Fprintf(&sb, "| **Subjects** | `a2a.v1.tasks.%s.%s` → `a2a.v1.events.%s.%s.>` |\n",
			addressee, rec.TaskID, addressee, rec.TaskID)
		breakdown := formatEnvelopeKindBreakdown(rec.EnvelopeCountsByKind)
		if breakdown != "" {
			fmt.Fprintf(&sb, "| **Envelopes Folded** | `%d` total (%s) |\n", rec.EnvelopeCount, breakdown)
		} else {
			fmt.Fprintf(&sb, "| **Envelopes Folded** | `%d` total |\n", rec.EnvelopeCount)
		}
		startStr := "—"
		if !rec.CreatedAt.IsZero() {
			startStr = rec.CreatedAt.UTC().Format("15:04:05 UTC")
		}
		endStr := "—"
		if !rec.UpdatedAt.IsZero() {
			endStr = rec.UpdatedAt.UTC().Format("15:04:05 UTC")
		}
		fmt.Fprintf(&sb, "| **Timing** | `%s` → `%s` (`%ds` elapsed) |\n", startStr, endStr, sec)
		if rec.DelegatedFromTaskID != "" || rec.WakeFromChildTaskID != "" {
			fmt.Fprintf(&sb, "| **Delegation Lineage** | DelegatedFrom: `%s`, WakeFromChild: `%s` |\n",
				firstNonEmptyStr(rec.DelegatedFromTaskID, "—"), firstNonEmptyStr(rec.WakeFromChildTaskID, "—"))
		}
		if len(rec.SteerTimestamps) > 0 {
			var steerTimes []string
			for _, ts := range rec.SteerTimestamps {
				steerTimes = append(steerTimes, fmt.Sprintf("`%s`", ts.UTC().Format("15:04:05 UTC")))
			}
			fmt.Fprintf(&sb, "| **Steer / Follow-Up Timeline** | `%d` steer(s) at %s |\n",
				len(rec.SteerTimestamps), strings.Join(steerTimes, ", "))
		}
		sb.WriteString("\n</details>\n\n")

		// Panel 2: Kanban Specialist Cards (when any specialist cards were created)
		cards, doneCards, runningCards, totalCards, _ := extractKanbanCardsFromTask(rec)
		if totalCards > 0 {
			fmt.Fprintf(&sb, "<details open>\n<summary>🗂️ <strong>Kanban Specialist Cards</strong> — <code>%d/%d completed</code>", doneCards, totalCards)
			if runningCards > 0 {
				fmt.Fprintf(&sb, ", <code>%d running</code>", runningCards)
			}
			sb.WriteString("</summary>\n\n")
			sb.WriteString("| Card ID | Cluster / Title | Worker / Session | Status | Verdict & Summary |\n")
			sb.WriteString("| :--- | :--- | :--- | :--- | :--- |\n")
			for _, c := range cards {
				titleCell := firstNonEmptyStr(c.Cluster, c.Title, "—")
				if c.Cluster != "" && c.Title != "" && !strings.EqualFold(c.Cluster, c.Title) {
					titleCell = fmt.Sprintf("**`%s`** — %s", c.Cluster, truncateText(c.Title, 64))
				} else {
					titleCell = fmt.Sprintf("**`%s`**", titleCell)
				}
				workerCell := firstNonEmptyStr(c.Worker, "specialist")
				if c.SessionID != "" {
					workerCell = fmt.Sprintf("`%s` (`%s`)", workerCell, c.SessionID)
				} else {
					workerCell = fmt.Sprintf("`%s`", workerCell)
				}
				statusBadge := "⏳ `pending`"
				switch c.Status {
				case "done":
					statusBadge = "✅ `done`"
				case "failed":
					statusBadge = "❌ `failed`"
				case "running":
					statusBadge = "🔄 `running`"
				}
				verdictSummary := firstNonEmptyStr(c.Summary, "—")
				if c.Verdict != "" {
					verdictSummary = fmt.Sprintf("`%s` — %s", c.Verdict, firstNonEmptyStr(c.Summary, "completed"))
				}
				verdictSummary = strings.ReplaceAll(strings.ReplaceAll(verdictSummary, "\n", " "), "|", "\\|")
				fmt.Fprintf(&sb, "| `%s` | %s | %s | %s | %s |\n",
					c.ID,
					strings.ReplaceAll(titleCell, "|", "\\|"),
					strings.ReplaceAll(workerCell, "|", "\\|"),
					statusBadge,
					truncateText(verdictSummary, 180))
			}
			sb.WriteString("\n</details>\n\n")
		}
	}

	if debug && len(rec.Activities) > 0 {
		fmt.Fprintf(&sb, "<details open>\n<summary>🛠️ <strong>Full Tool Activity Stream</strong> — <code>%d tool calls</code></summary>\n\n", len(rec.Activities))
		sb.WriteString("#### 🛠️ Tool Activity Stream\n\n")
		sb.WriteString("| # | Time | Worker / Card | Tool | Status | Duration | Arguments | Result / Error Preview |\n")
		sb.WriteString("| ---: | :--- | :--- | :--- | :--- | ---: | :--- | :--- |\n")
		maxInputChars := 240
		maxPreviewChars := 180
		if len(rec.Activities) > 24 {
			maxInputChars = 75
			maxPreviewChars = 65
		}
		const (
			maxDebugTableRows = 24
			headDebugRows     = 8
			tailDebugRows     = 16
		)
		for i, act := range rec.Activities {
			if len(rec.Activities) > maxDebugTableRows && i >= headDebugRows && i < len(rec.Activities)-tailDebugRows {
				if i == headDebugRows {
					omitted := len(rec.Activities) - headDebugRows - tailDebugRows
					fmt.Fprintf(&sb, "| … | — | — | *(%d intermediate tool calls)* | — | — | *See per-step expandable cards above* | — |\n", omitted)
				}
				continue
			}
			idx := act.Index
			if idx <= 0 {
				idx = i + 1
			}
			timeStr := "—"
			if tStr := formatActivityAtTime(act.At); tStr != "" {
				timeStr = fmt.Sprintf("`%s`", tStr)
			}
			workerStr := firstNonEmptyStr(act.Worker, "platform")
			if act.SessionID != "" && !strings.Contains(workerStr, act.SessionID) {
				workerStr = fmt.Sprintf("`%s` (`%s`)", workerStr, act.SessionID)
			} else {
				workerStr = fmt.Sprintf("`%s`", workerStr)
			}
			statusStr := act.Status
			if statusStr == "" {
				statusStr = "called"
			}
			durStr := "—"
			if act.DurationMs > 0 {
				if act.DurationMs >= 1000 {
					durStr = fmt.Sprintf("%dms (%.1fs)", act.DurationMs, float64(act.DurationMs)/1000.0)
				} else {
					durStr = fmt.Sprintf("%dms", act.DurationMs)
				}
			}
			inputClean := strings.ReplaceAll(strings.TrimSpace(act.Input), "\n", " ")
			inputClean = strings.ReplaceAll(inputClean, "|", "\\|")
			inputClean = truncateText(inputClean, maxInputChars)

			previewClean := "—"
			if act.ErrorMessage != "" {
				errClean := strings.ReplaceAll(strings.TrimSpace(act.ErrorMessage), "\n", " ")
				errClean = strings.ReplaceAll(errClean, "|", "\\|")
				previewClean = fmt.Sprintf("❌ `%s`", truncateText(errClean, maxPreviewChars))
			} else if act.ResultPreview != "" {
				resClean := strings.ReplaceAll(strings.TrimSpace(act.ResultPreview), "\n", " ")
				resClean = strings.ReplaceAll(resClean, "|", "\\|")
				previewClean = fmt.Sprintf("`%s`", truncateText(resClean, maxPreviewChars))
			}
			fmt.Fprintf(&sb, "| %d | %s | %s | `%s` | `%s` | %s | `%s` | %s |\n",
				idx, timeStr, strings.ReplaceAll(workerStr, "|", "\\|"), act.Tool, statusStr, durStr, firstNonEmptyStr(inputClean, "—"), previewClean)
		}
		sb.WriteString("\n</details>\n\n")
	}

	var thinkingLines []string
	for _, raw := range strings.Split(rec.Thinking, "\n") {
		if strings.TrimSpace(raw) != "" {
			thinkingLines = append(thinkingLines, strings.TrimSpace(raw))
		}
	}
	if debug && (len(thinkingLines) > 0 || len(rec.IntermediateTurns) > 0) {
		fmt.Fprintf(&sb, "<details>\n<summary>🧠 <strong>Raw LLM API Turn Log</strong> — <code>%d thinking frames</code></summary>\n\n", len(thinkingLines))
		const (
			maxDebugThinkingLines = 16
			headThinkingLines     = 6
			tailThinkingLines     = 10
		)
		for i, raw := range thinkingLines {
			if len(thinkingLines) > maxDebugThinkingLines && i >= headThinkingLines && i < len(thinkingLines)-tailThinkingLines {
				if i == headThinkingLines {
					omitted := len(thinkingLines) - headThinkingLines - tailThinkingLines
					fmt.Fprintf(&sb, "- *… (%d intermediate thinking frames omitted — see consolidated steps above)*\n", omitted)
				}
				continue
			}
			tTitle, tThought := normalizeStepTitleAndThought(raw)
			cleanRaw := raw
			if tTitle != "" {
				if tThought != "" {
					cleanRaw = tTitle + " — " + tThought
				} else {
					cleanRaw = tTitle
				}
			} else {
				cleanRaw = strings.ReplaceAll(raw, "\t", " — ")
			}
			clean := truncateText(cleanRaw, 150)
			fmt.Fprintf(&sb, "%d. %s\n", i+1, clean)
		}
		if len(rec.IntermediateTurns) > 0 {
			sb.WriteString("\n#### 🔄 Intermediate Turns\n")
			for i, t := range rec.IntermediateTurns {
				if i >= 2 {
					fmt.Fprintf(&sb, "*… (%d additional intermediate turns omitted)*\n\n", len(rec.IntermediateTurns)-2)
					break
				}
				fmt.Fprintf(&sb, "> %s\n\n", truncateText(strings.TrimSpace(t), 450))
			}
		}
		sb.WriteString("\n</details>\n\n")
	}

	if debug && len(rec.Delegates) > 0 {
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

	if debug && len(rec.Notices) > 0 {
		sb.WriteString("#### 📌 Task Notices\n")
		for _, n := range rec.Notices {
			fmt.Fprintf(&sb, "- %s\n", n)
		}
		sb.WriteString("\n")
	}

	body := renderTaskAnswerBody(rec)
	if body != "" {
		if debug {
			sb.WriteString("#### 🎯 Result\n\n")
		}
		sb.WriteString(body + "\n")
	}

	return sb.String()
}

// SynthesizeThinkingFromActivities builds a clean human-readable thinking trace when a task
// executed tool calls or progress updates without explicit thinking artifact chunks.
func SynthesizeThinkingFromActivities(rec *TaskRecord) string {
	if rec == nil {
		return ""
	}
	var lines []string
	addLine := func(s string) {
		s = strings.TrimSpace(s)
		if s == "" {
			return
		}
		if len(lines) == 0 || lines[len(lines)-1] != s {
			lines = append(lines, s)
		}
	}
	for _, act := range rec.Activities {
		for _, toolName := range expandActivityToolNames(act) {
			addLine(toolNameToStepTitle(toolName))
		}
	}
	prog := strings.TrimSpace(rec.ProgressText)
	res := strings.TrimSpace(rec.ResultText)
	if prog != "" {
		if res != "" && (prog == res || strings.Contains(res, prog)) {
			if len(lines) == 0 {
				if rec.WakeFromChildTaskID != "" {
					addLine(synthFinalStepTitle + "\tSynthesizing delegated task results into final response.")
				} else {
					addLine(synthFinalStepTitle)
				}
			}
		} else {
			addLine(prog)
		}
	} else if len(lines) == 0 && res != "" && rec.WakeFromChildTaskID != "" {
		addLine(synthFinalStepTitle + "\tSynthesizing delegated task results into final response.")
	}
	return strings.Join(lines, "\n")
}

// FormatLiveTaskProgress renders a concise single-line live status message suitable for
// MCP notifications/progress frames when debug mode is enabled.
func FormatLiveTaskProgress(primary *TaskRecord, children []*TaskRecord, wake *TaskRecord, elapsed time.Duration) string {
	if primary == nil {
		return "Waiting for kube-agents task..."
	}
	rounded := elapsed.Round(time.Second)
	if rounded < time.Second {
		rounded = time.Second
	}
	msg := fmt.Sprintf("Task %s → %s (%s, %s elapsed, %d tool calls)",
		primary.TaskID, primary.Addressee, primary.State, rounded, len(primary.Activities))
	if len(children) > 0 {
		lastChild := children[len(children)-1]
		msg += fmt.Sprintf(" • Delegated to %s (%s)", lastChild.Addressee, lastChild.State)
	}
	if wake != nil {
		msg += fmt.Sprintf(" • Wake synthesis (%s)", wake.State)
	}
	return msg
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

	// Mark is reaction state (the kind of ask, e.g. "fleet", and how it settled), not a question
	// for the user: it must not render as "Waiting for input". Input-required is conveyed by the
	// task state badge and the status message instead.

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
