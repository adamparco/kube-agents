package mcp

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

var (
	legacyStepPrefixRe        = regexp.MustCompile(`^Step\s+\d+:\s*`)
	legacyPlanPrefixRe        = regexp.MustCompile(`(?i)^Planning\s*&\s*executing\s+\d+\s+tool\s+call\(s\)\s*[—-]\s*`)
	legacyActionPrefixRe      = regexp.MustCompile(`^→\s*Action:\s*`)
	legacySynthPrefixRe       = regexp.MustCompile(`(?i)^Synthesizing findings from \d+ completed tool call\(s\) into final response\.?$`)
	legacyKeyBracketSufRe     = regexp.MustCompile(`\s*\[[A-Za-z0-9_,\s-]+\]$`)
	shapedStringPlaceholderRe = regexp.MustCompile(`^<string,\s*(\d+)\s*chars>$`)
	kanbanResultIDRe          = regexp.MustCompile(`\bid=([A-Za-z0-9_-]+)`)
	specialistStepHeaderRe    = regexp.MustCompile(`(?i)^Specialist check completed:\s*(.+?)\s*\(([^()—-]+?)\s*[—-]\s*(\d+)/(\d+)\s+cards\s+done\)\.?$`)
	profileRegionSuffixRe     = regexp.MustCompile(`-(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-[a-z0-9-]+$`)
	fleetWideParenSuffixRe    = regexp.MustCompile(`(?i)\s*\(fleet-wide [^)]*(?:\([^.)]*\))?[^.)]*\)?\.?$`)
	trailingClusterParenRe    = regexp.MustCompile(`\s*\(([a-z0-9][a-z0-9_-]{2,47})\)\.?$`)
)

const (
	cardResultsRawHeader        = "Specialist card results (raw):"
	silentResultFallback        = "_The agent completed this turn without a textual reply._"
	synthFinalStepTitle         = "Synthesizing findings into final response."
	streamingStepContinuePrefix = "STREAMING_STEP_CONTINUE:"
	maxStepActivitiesShown      = 6
	maxDebugStepActivitiesShown = 12
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
// across the primary task, delegated child tasks, and wake synthesis task.
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
	for _, sep := range []string{" — ", " - ", ": ", "; "} {
		if idx := strings.Index(s, sep); idx > 12 {
			s = s[:idx]
			break
		}
	}
	s = strings.TrimSuffix(strings.TrimSpace(s), ".")
	return truncateText(s, cardHeadlineMaxChars)
}

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
	lowerTitle := strings.ToLower(strings.TrimSpace(stepTitle))
	for _, toolName := range expandActivityToolNames(act) {
		cand := strings.ToLower(toolNameToStepTitle(toolName))
		if verb, _, ok := strings.Cut(cand, " "); ok && len(verb) >= 4 && strings.HasPrefix(lowerTitle, verb+" ") {
			return true
		}
		switch {
		case toolName == "terminal" || toolName == "kubectl" || toolName == "run_kubectl" || toolName == "bash":
			if strings.HasPrefix(lowerTitle, "checking ") || strings.HasPrefix(lowerTitle, "inspecting ") || strings.HasPrefix(lowerTitle, "running ") {
				return true
			}
		case strings.HasPrefix(toolName, "kanban_"):
			if strings.Contains(lowerTitle, "kanban") || strings.Contains(lowerTitle, "specialist") {
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

var staticToolStepTitles = map[string]string{
	"mcp__gke__list_clusters":                      "Listing GKE clusters in the fleet.",
	"mcp__gke__get_cluster":                        "Inspecting GKE cluster configuration.",
	"mcp__platform_control__list_cluster_profiles": "Listing GKE cluster profiles in the fleet.",
	"read_file":          "Reading file in the workspace.",
	"search_files":       "Inspecting files in the workspace.",
	"find_by_name":       "Inspecting files in the workspace.",
	"list_dir":           "Inspecting files in the workspace.",
	"glob":               "Inspecting files in the workspace.",
	"patch":              "Editing a file in the agent workspace.",
	"edit_file":          "Editing a file in the agent workspace.",
	"replace_in_file":    "Editing a file in the agent workspace.",
	"write_file":         "Writing a file in the agent workspace.",
	"create_file":        "Writing a file in the agent workspace.",
	"terminal":           "Running cluster and workspace diagnostics.",
	"kubectl":            "Running cluster and workspace diagnostics.",
	"run_kubectl":        "Running cluster and workspace diagnostics.",
	"bash":               "Running cluster and workspace diagnostics.",
	"kanban_create":      "Delegating task to specialist agent.",
	"tool_search":        "Searching available tools.",
	"tool_describe":      "Inspecting tool schema.",
	"execute_code":       "Running analysis script.",
	"skill_view":         "Loading skill instructions.",
	"mcp__a2a__delegate": "Delegating task to platform.",
	"web_search":         "Searching the web.",
	"web_extract":        "Reading web page content.",
	"web_fetch":          "Reading web page content.",
	"read_url_content":   "Reading web page content.",
}

var prefixToolStepTitles = []struct {
	prefix string
	title  string
}{
	{"mcp__gke__", "Querying GKE cluster state."},
	{"mcp__platform_control__", "Querying platform control state."},
	{"kanban_", "Updating task status on the Kanban board."},
	{"mcp__a2a__", "Communicating over the A2A bus."},
	{"mcp__developer_knowledge__", "Searching documentation in Developer Knowledge."},
}

func toolNameToStepTitle(toolName string) string {
	if title, ok := staticToolStepTitles[toolName]; ok {
		return title
	}
	for _, p := range prefixToolStepTitles {
		if strings.HasPrefix(toolName, p.prefix) {
			return p.title
		}
	}
	if strings.HasPrefix(toolName, "mcp__") {
		parts := strings.SplitN(strings.TrimPrefix(toolName, "mcp__"), "__", 2)
		if len(parts) == 2 && parts[0] != "" && parts[1] != "" {
			return fmt.Sprintf("Calling %s (%s).", strings.ReplaceAll(parts[1], "_", " "), parts[0])
		}
	}
	return fmt.Sprintf("Running %s.", toolName)
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

// FormatTurnCustomToolEnvelope builds the Jetski _custom_tool_result fields (title, markdown, result)
// for a turn outcome.
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

// FormatStepDetailMarkdown renders the rich expandable markdown body for a single step row.
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
	// Structural multi-sentence split: when an untabbed line contains multiple sentences,
	// use the first sentence as the concise step title and preserve the full text as the thought.
	if idx := strings.Index(cleaned, ". "); idx > 0 && idx <= 72 {
		return strings.TrimSpace(cleaned[:idx+1]), cleaned
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
		return synthFinalStepTitle
	}
	if strings.HasPrefix(line, "Running ") && strings.HasSuffix(line, ".") {
		toolName := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(line, "Running "), "."))
		if toolName != "" && !strings.Contains(toolName, " ") {
			return toolNameToStepTitle(toolName)
		}
	}
	return strings.TrimSpace(line)
}
