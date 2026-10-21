package mcpbridge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	swarmDirName           = "swarm"
	swarmMembersFileName   = "members.json"
	swarmBoardFileName     = "board.jsonl"
	swarmViewJSONFileName  = "view.json"
	swarmViewHTMLFileName  = "view.html"
	swarmMetaFileName      = "swarm.json"
	swarmLedgerFileName    = "kube_agents_swarm_state.json"
	swarmLockFileName      = "kube_agents_swarm.lock"
	swarmDefaultTitle      = "kube-agents swarm"
	swarmLedgerSchema      = 8
	swarmManagedByTag      = "kube-agents"
	swarmRoleCoordinator   = "coordinator"
	swarmRoleNarrator      = "narrator"
	swarmRoleWorker        = "worker"
	swarmWakeMentions      = "mentions"
	swarmWakeWorking       = "working"
	swarmMemberCoordinator = "coordinator"
	swarmMemberNarrator    = "narrator"
	swarmMemberPlatform    = "platform"
	swarmPruneCooldown     = 15 * time.Second
)

// SwarmSink persists projected kube-agents fleet state into Antigravity's local Swarm UI directory.
type SwarmSink interface {
	Sync(ctx context.Context, input SwarmProjectionInput) error
	ApplyRemoteSnapshot(ctx context.Context, rootID, brainDir string, resp RemoteSwarmResponse) error
}

// SwarmProjectionInput holds local conversation and deployment inputs for offline/local
// Swarm roster projection when the remote gateway /mcp/swarm endpoint has not been queried.
type SwarmProjectionInput struct {
	RootConversationID    string
	BrainDir              string
	Deployment            DeploymentProfile
	DiscoveredDeployments []DeploymentProfile
	Connected             bool
	ActiveConversation    string
	PushArmed             bool
	Conversations         map[string]ConversationEntry
	Subagents             map[string]ConversationLineage
	Now                   time.Time
}

// SwarmRosterFile represents the top-level .system_generated/swarm/members.json document
// parsed by Antigravity's native Swarm UI sidecar.
type SwarmRosterFile struct {
	Title   string                 `json:"title"`
	Members map[string]SwarmMember `json:"members"`
}

// SwarmMember represents a single entry in .system_generated/swarm/members.json.
type SwarmMember struct {
	Name               string `json:"name"`
	Role               string `json:"role"`
	Model              string `json:"model,omitempty"`
	ConversationID     string `json:"conversation_id"`
	WakeMode           string `json:"wake_mode"`
	WaitSince          int    `json:"wait_since"`
	LastReadSeq        int    `json:"last_read_seq"`
	LastNudgedSeq      int    `json:"last_nudged_seq"`
	LastParkBlockedSeq int    `json:"last_park_blocked_seq"`
	JoinedAt           string `json:"joined_at,omitempty"`
	Killed             bool   `json:"killed"`
	ManagedBy          string `json:"managed_by,omitempty"`
}

// SwarmBoardMessage represents a single line in .system_generated/swarm/board.jsonl.
type SwarmBoardMessage struct {
	Seq   int    `json:"seq"`
	Agent string `json:"agent"`
	Text  string `json:"text"`
	Time  string `json:"time"`
}

// SwarmRef cites a board post (#seq) or a roster member in view.json.
type SwarmRef struct {
	Seq    int    `json:"seq,omitempty"`
	Member string `json:"member,omitempty"`
}

// SwarmAction represents a validated navigation button in view.json.
type SwarmAction struct {
	Type   string `json:"type"`
	Target any    `json:"target"`
	Label  string `json:"label"`
}

// SwarmPhase describes the current high-level phase in view.json.
type SwarmPhase struct {
	Label string `json:"label"`
	Index int    `json:"index"`
	Total int    `json:"total"`
}

// SwarmAttentionItem represents an item in view.json's attention list.
type SwarmAttentionItem struct {
	ID       string        `json:"id"`
	Kind     string        `json:"kind"`
	Severity string        `json:"severity"`
	Title    string        `json:"title"`
	Why      string        `json:"why,omitempty"`
	Refs     []SwarmRef    `json:"refs"`
	Actions  []SwarmAction `json:"actions,omitempty"`
}

// SwarmWorkstream represents an item in view.json's workstreams list.
type SwarmWorkstream struct {
	ID      string        `json:"id"`
	Title   string        `json:"title"`
	Owners  []string      `json:"owners"`
	State   string        `json:"state"`
	Note    string        `json:"note,omitempty"`
	Refs    []SwarmRef    `json:"refs"`
	Actions []SwarmAction `json:"actions,omitempty"`
}

// SwarmTextItem represents a milestone or risk item in view.json.
type SwarmTextItem struct {
	ID      string        `json:"id"`
	Text    string        `json:"text"`
	Refs    []SwarmRef    `json:"refs"`
	Actions []SwarmAction `json:"actions,omitempty"`
}

// SwarmView represents the structured .system_generated/swarm/view.json document.
type SwarmView struct {
	Version     int                      `json:"version"`
	AsOfSeq     int                      `json:"as_of_seq"`
	UpdatedAt   string                   `json:"updated_at"`
	Headline    string                   `json:"headline"`
	Phase       *SwarmPhase              `json:"phase,omitempty"`
	Attention   []SwarmAttentionItem     `json:"attention"`
	Workstreams []SwarmWorkstream        `json:"workstreams"`
	Milestones  []SwarmTextItem          `json:"milestones"`
	Risks       []SwarmTextItem          `json:"risks"`
	KubeAgents  *KubeAgentsOverviewState `json:"kube_agents,omitempty"`
}

// BoardLedger tracks already-emitted event keys and their assigned board sequence numbers
// so board.jsonl is strictly append-only and idempotent across syncs and restarts.
type BoardLedger struct {
	SchemaVersion int             `json:"schemaVersion,omitempty"`
	NextSeq       int             `json:"nextSeq"`
	EventSeqs     map[string]int  `json:"eventSeqs"`
	Authors       map[string]bool `json:"authors,omitempty"`
}

// SwarmSnapshot is the complete projected state produced by the remote gateway or local fallback.
type SwarmSnapshot struct {
	Title       string                 `json:"title"`
	Members     map[string]SwarmMember `json:"members"`
	NewMessages []SwarmBoardMessage    `json:"newMessages,omitempty"`
	View        SwarmView              `json:"view"`
	ViewHTML    string                 `json:"viewHtml,omitempty"`
}

// RemoteSwarmRequest is sent by the local workstation bridge to POST /mcp/swarm on the
// remote a2a-gateway to synchronize conversation state, compute the projected SwarmSnapshot
// against the gateway's live BusSession, and drain pending push events.
type RemoteSwarmRequest struct {
	RootConversationID string                         `json:"rootConversationId"`
	Deployment         DeploymentProfile              `json:"deployment"`
	Conversations      map[string]ConversationEntry   `json:"conversations,omitempty"`
	ActiveConversation string                         `json:"activeConversation,omitempty"`
	Subagents          map[string]ConversationLineage `json:"subagents,omitempty"`
	Ledger             BoardLedger                    `json:"ledger"`
	ExistingBoardLines []string                       `json:"existingBoardLines,omitempty"`
	DrainEventsFor     string                         `json:"drainEventsFor,omitempty"`
}

// RemoteSwarmResponse is returned by POST /mcp/swarm from the remote a2a-gateway.
type RemoteSwarmResponse struct {
	Connected          bool                         `json:"connected"`
	MigratedSchema     bool                         `json:"migratedSchema,omitempty"`
	Deployment         DeploymentProfile            `json:"deployment"`
	Snapshot           SwarmSnapshot                `json:"snapshot"`
	Ledger             BoardLedger                  `json:"ledger"`
	Conversations      map[string]ConversationEntry `json:"conversations,omitempty"`
	ActiveConversation string                       `json:"activeConversation,omitempty"`
	PendingEvents      []PushEvent                  `json:"pendingEvents,omitempty"`
}

// AGYSwarmSink writes projected Swarm state into <brainDir>/<rootConversationID>/.system_generated/swarm/
// using atomic file renames and advisory file locking.
type AGYSwarmSink struct {
	mu              sync.Mutex
	lastPruneRootID string
	lastPruneAt     time.Time
}

// NewAGYSwarmSink constructs a AGYSwarmSink.
func NewAGYSwarmSink() *AGYSwarmSink {
	return &AGYSwarmSink{}
}

// ReadLocalSwarmState reads the persisted BoardLedger and existing board.jsonl lines for rootID
// so the bridge can pass them in a RemoteSwarmRequest to POST /mcp/swarm.
func ReadLocalSwarmState(brainDir, rootID string) (BoardLedger, []string) {
	bDir := strings.TrimSpace(brainDir)
	rID := strings.TrimSpace(rootID)
	if bDir == "" || rID == "" {
		return BoardLedger{SchemaVersion: swarmLedgerSchema, NextSeq: 1, EventSeqs: make(map[string]int)}, nil
	}
	swarmDir := filepath.Join(bDir, rID, systemGeneratedDirName, swarmDirName)
	ledger := readBoardLedger(filepath.Join(swarmDir, swarmLedgerFileName))
	lines, maxSeq := readExistingBoardLines(filepath.Join(swarmDir, swarmBoardFileName))
	if maxSeq >= ledger.NextSeq {
		ledger.NextSeq = maxSeq + 1
	}
	return ledger, lines
}

// ApplyRemoteSnapshot atomically writes a RemoteSwarmResponse computed by the in-cluster
// gateway to <brainDir>/<rootID>/.system_generated/swarm/.
func (s *AGYSwarmSink) ApplyRemoteSnapshot(ctx context.Context, rootID, brainDir string, resp RemoteSwarmResponse) error {
	if ctx != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	rID := strings.TrimSpace(rootID)
	bDir := strings.TrimSpace(brainDir)
	if rID == "" || bDir == "" || !IsAGYConversationAlive(bDir, rID) {
		return nil
	}
	if NewAGYLineageResolver(bDir).ResolveLineage(rID, "").IsSubagent {
		return nil
	}
	return s.writeSnapshotFiles(rID, bDir, resp.Snapshot, resp.Ledger, resp.MigratedSchema)
}

// Sync projects input locally (offline fallback when not connected to a remote gateway) and
// atomically updates the Swarm directory for input.RootConversationID.
func (s *AGYSwarmSink) Sync(ctx context.Context, input SwarmProjectionInput) error {
	if ctx != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	rootID := strings.TrimSpace(input.RootConversationID)
	brainDir := strings.TrimSpace(input.BrainDir)
	if rootID == "" || brainDir == "" {
		return nil
	}
	if rootID != defaultDisconnectedSwarmID {
		if !IsAGYConversationAlive(brainDir, rootID) {
			return nil
		}
		if NewAGYLineageResolver(brainDir).ResolveLineage(rootID, "").IsSubagent {
			return nil
		}
	}
	prevLedger, existingLines := ReadLocalSwarmState(brainDir, rootID)
	migrating := prevLedger.SchemaVersion > 0 && prevLedger.SchemaVersion < swarmLedgerSchema
	if prevLedger.SchemaVersion == 0 && len(prevLedger.EventSeqs) > 0 {
		migrating = true
	}
	if migrating {
		prevLedger = BoardLedger{
			SchemaVersion: swarmLedgerSchema,
			NextSeq:       1,
			EventSeqs:     make(map[string]int),
			Authors:       make(map[string]bool),
		}
		existingLines = nil
	}
	snap, nextLedger := buildLocalSwarmState(input, prevLedger)
	if !migrating && len(existingLines) > 0 && boardHasOrphanedManagedAuthor(existingLines, prevLedger, snap.Members) {
		migrating = true
		prevLedger = BoardLedger{
			SchemaVersion: swarmLedgerSchema,
			NextSeq:       1,
			EventSeqs:     make(map[string]int),
			Authors:       make(map[string]bool),
		}
		snap, nextLedger = buildLocalSwarmState(input, prevLedger)
	}
	return s.writeSnapshotFiles(rootID, brainDir, snap, nextLedger, migrating)
}

// SyncDefaultDisconnectedOverview keeps <brainDir>/_kube_agents_default/.system_generated/swarm/
// up to date with the current Connected or Not-Connected Overview tab so the Swarm UI can
// immediately display the Overview in any conversation before connect_deployment is called.
func SyncDefaultDisconnectedOverview(ctx context.Context, brainDir string, store *ConfigStore) {
	bDir := strings.TrimSpace(brainDir)
	if bDir == "" {
		return
	}
	var discovered []DeploymentProfile
	var dep DeploymentProfile
	var convs map[string]ConversationEntry
	var activeConv string
	connected := false
	pushArmed := false
	if store != nil {
		snapCfg := store.Snapshot()
		convs = snapCfg.Conversations
		activeConv = snapCfg.ActiveConversation
		pushArmed = snapCfg.Push.Enabled
		for _, p := range snapCfg.Deployments {
			discovered = append(discovered, p)
		}
		if activeDep, ok := store.ActiveDeployment(); ok {
			dep = activeDep
		}
		connected = store.IsDefaultOverviewConnected("")
	}
	if dep.LatestReleaseTag == "" {
		EnrichDeploymentVersionStatus(ctx, &dep)
	}
	input := SwarmProjectionInput{
		RootConversationID:    defaultDisconnectedSwarmID,
		BrainDir:              bDir,
		Deployment:            dep,
		DiscoveredDeployments: discovered,
		Connected:             connected,
		ActiveConversation:    activeConv,
		PushArmed:             pushArmed,
		Conversations:         convs,
		Now:                   time.Now().UTC(),
	}
	_ = NewAGYSwarmSink().Sync(ctx, input)
}

func (s *AGYSwarmSink) writeSnapshotFiles(
	rootID string,
	brainDir string,
	snap SwarmSnapshot,
	nextLedger BoardLedger,
	migratingSchema bool,
) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	swarmDir := filepath.Join(brainDir, rootID, systemGeneratedDirName, swarmDirName)
	if err := os.MkdirAll(swarmDir, dirPermMode); err != nil {
		return fmt.Errorf("creating swarm dir %s: %w", swarmDir, err)
	}

	lockPath := filepath.Join(swarmDir, swarmLockFileName)
	unlock, lockErr := lockFile(lockPath)
	if lockErr == nil {
		defer unlock()
	}

	ledgerPath := filepath.Join(swarmDir, swarmLedgerFileName)
	boardPath := filepath.Join(swarmDir, swarmBoardFileName)
	membersPath := filepath.Join(swarmDir, swarmMembersFileName)
	viewJSONPath := filepath.Join(swarmDir, swarmViewJSONFileName)
	viewHTMLPath := filepath.Join(swarmDir, swarmViewHTMLFileName)
	metaPath := filepath.Join(swarmDir, swarmMetaFileName)

	existingBoardLines, _ := readExistingBoardLines(boardPath)
	if migratingSchema {
		existingBoardLines = nil
	}

	mergedMembers := readExistingUnmanagedMembers(membersPath)
	for k, v := range snap.Members {
		mergedMembers[k] = v
	}

	title := firstNonEmptyStr(snap.Title, swarmDefaultTitle)
	rosterDoc := SwarmRosterFile{
		Title:   title,
		Members: mergedMembers,
	}
	membersBytes, err := json.MarshalIndent(rosterDoc, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileAtomicIfChanged(membersPath, membersBytes); err != nil {
		return err
	}

	if len(snap.NewMessages) > 0 || len(existingBoardLines) == 0 || migratingSchema {
		var boardBuf bytes.Buffer
		for _, line := range existingBoardLines {
			boardBuf.WriteString(line)
			boardBuf.WriteByte('\n')
		}
		for _, msg := range snap.NewMessages {
			raw, mErr := json.Marshal(msg)
			if mErr != nil {
				continue
			}
			boardBuf.Write(raw)
			boardBuf.WriteByte('\n')
		}
		if err := writeFileAtomicIfChanged(boardPath, boardBuf.Bytes()); err != nil {
			return err
		}
	}

	viewBytes, err := json.MarshalIndent(snap.View, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileAtomicIfChanged(viewJSONPath, viewBytes); err != nil {
		return err
	}

	if snap.ViewHTML != "" {
		_ = writeFileAtomicIfChanged(viewHTMLPath, []byte(snap.ViewHTML))
	}

	metaBytes, err := json.MarshalIndent(map[string]string{"title": title}, "", "  ")
	if err == nil {
		_ = writeFileAtomicIfChanged(metaPath, metaBytes)
	}

	ledgerBytes, err := json.MarshalIndent(nextLedger, "", "  ")
	if err == nil {
		_ = writeFileAtomicIfChanged(ledgerPath, ledgerBytes)
	}

	if rootID != defaultDisconnectedSwarmID && snap.View.KubeAgents != nil {
		defDir := filepath.Join(brainDir, defaultDisconnectedSwarmID, systemGeneratedDirName, swarmDirName)
		if os.MkdirAll(defDir, dirPermMode) == nil {
			defMembers := make(map[string]SwarmMember, len(snap.Members))
			for k, v := range snap.Members {
				if v.ConversationID == rootID {
					v.ConversationID = ""
				}
				defMembers[k] = v
			}
			defRosterDoc := SwarmRosterFile{
				Title:   title,
				Members: defMembers,
			}
			if defMembersBytes, mErr := json.MarshalIndent(defRosterDoc, "", "  "); mErr == nil {
				_ = writeFileAtomicIfChanged(filepath.Join(defDir, swarmMembersFileName), defMembersBytes)
			}
			_ = writeFileAtomicIfChanged(filepath.Join(defDir, swarmViewJSONFileName), viewBytes)
			if snap.ViewHTML != "" {
				_ = writeFileAtomicIfChanged(filepath.Join(defDir, swarmViewHTMLFileName), []byte(snap.ViewHTML))
			}
			if len(metaBytes) > 0 {
				_ = writeFileAtomicIfChanged(filepath.Join(defDir, swarmMetaFileName), metaBytes)
			}
			if boardData, bErr := os.ReadFile(boardPath); bErr == nil && len(boardData) > 0 {
				_ = writeFileAtomicIfChanged(filepath.Join(defDir, swarmBoardFileName), boardData)
			}
		}
	}

	if s.lastPruneRootID != rootID || time.Since(s.lastPruneAt) >= swarmPruneCooldown {
		PruneStaleKubeAgentsSwarms(brainDir, rootID, mergedMembers)
		s.lastPruneRootID = rootID
		s.lastPruneAt = time.Now()
	}
	return nil
}

func buildLocalSwarmState(input SwarmProjectionInput, prevLedger BoardLedger) (SwarmSnapshot, BoardLedger) {
	now := input.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}
	ledger := BoardLedger{
		SchemaVersion: swarmLedgerSchema,
		NextSeq:       prevLedger.NextSeq,
		EventSeqs:     make(map[string]int, len(prevLedger.EventSeqs)),
		Authors:       make(map[string]bool, len(prevLedger.Authors)),
	}
	if ledger.NextSeq <= 0 {
		ledger.NextSeq = 1
	}
	for k, v := range prevLedger.EventSeqs {
		ledger.EventSeqs[k] = v
	}
	for k, v := range prevLedger.Authors {
		ledger.Authors[k] = v
	}

	depName := firstNonEmptyStr(input.Deployment.Name, input.Deployment.ClusterName, input.Deployment.PlatformAgentName, "kube-agents")
	title := fmt.Sprintf("kube-agents · %s", depName)
	if !input.Connected {
		title = "kube-agents · Not Connected"
	}
	nowISO := now.UTC().Format(time.RFC3339)
	head := ledger.NextSeq - 1
	if head < 0 {
		head = 0
	}

	coordCID := input.RootConversationID
	if coordCID == defaultDisconnectedSwarmID {
		coordCID = ""
	}
	members := map[string]SwarmMember{
		swarmMemberCoordinator: {
			Name:           swarmMemberCoordinator,
			Role:           swarmRoleCoordinator,
			Model:          "antigravity",
			ConversationID: coordCID,
			WakeMode:       swarmWakeMentions,
			WaitSince:      head,
			LastReadSeq:    head,
			JoinedAt:       nowISO,
			ManagedBy:      swarmManagedByTag,
		},
		swarmMemberNarrator: {
			Name:        swarmMemberNarrator,
			Role:        swarmRoleNarrator,
			Model:       "a2a-jetstream/0.4",
			WakeMode:    swarmWakeMentions,
			WaitSince:   head,
			LastReadSeq: head,
			JoinedAt:    nowISO,
			ManagedBy:   swarmManagedByTag,
		},
	}
	if input.Connected {
		members[swarmMemberPlatform] = SwarmMember{
			Name:           swarmMemberPlatform,
			Role:           swarmRoleWorker,
			Model:          "hermes / " + depName,
			ConversationID: coordCID,
			WakeMode:       swarmWakeMentions,
			WaitSince:      head,
			LastReadSeq:    head,
			JoinedAt:       nowISO,
			ManagedBy:      swarmManagedByTag,
		}
	}

	usedNames := map[string]bool{
		swarmMemberCoordinator: true,
		swarmMemberNarrator:    true,
		swarmMemberPlatform:    true,
	}
	rootID := strings.TrimSpace(input.RootConversationID)
	if input.Connected {
		aliases := make([]string, 0, len(input.Conversations))
		for alias := range input.Conversations {
			aliases = append(aliases, alias)
		}
		sort.Strings(aliases)
		for _, alias := range aliases {
			conv := input.Conversations[alias]
			if conv.SessionTerminated && !conv.SessionMode {
				continue
			}
			if !conv.SessionMode && !isSessionSubagentEntry(conv) && strings.TrimSpace(conv.SessionWorkerID) == "" {
				continue
			}
			if rootID != "" && strings.TrimSpace(conv.ParentAGYConversationID) != "" && strings.TrimSpace(conv.ParentAGYConversationID) != rootID {
				continue
			}
			var lin ConversationLineage
			if cid := strings.TrimSpace(conv.AGYConversationID); cid != "" && input.Subagents != nil {
				lin = input.Subagents[cid]
			}
			name := uniqueMemberName(deriveSessionMemberName(conv, lin), usedNames)
			members[name] = SwarmMember{
				Name:           name,
				Role:           swarmRoleWorker,
				Model:          "claude-code / session",
				ConversationID: strings.TrimSpace(conv.AGYConversationID),
				WakeMode:       swarmWakeMentions,
				WaitSince:      head,
				LastReadSeq:    head,
				JoinedAt:       nowISO,
				ManagedBy:      swarmManagedByTag,
			}
		}
	}

	var newMessages []SwarmBoardMessage
	if !input.Connected {
		discKey := "disconnected-welcome"
		if _, seen := ledger.EventSeqs[discKey]; !seen {
			seq := ledger.NextSeq
			ledger.NextSeq++
			ledger.EventSeqs[discKey] = seq
			ledger.Authors[swarmMemberNarrator] = true
			msgText := "@coordinator Welcome to **kube-agents** (Kubernetes Agentic Harness). Not connected to a cluster yet — open the **Overview** tab to connect to a discovered GKE cluster or start a guided installation (`https://github.com/gke-labs/kube-agents`)."
			if len(input.DiscoveredDeployments) > 0 {
				var names []string
				for _, p := range input.DiscoveredDeployments {
					if n := firstNonEmptyStr(p.Name, p.ClusterName, p.PlatformAgentName); n != "" {
						names = append(names, "`"+n+"`")
					}
				}
				if len(names) > 0 {
					msgText = fmt.Sprintf("@coordinator Discovered %d `kube-agents` deployment(s) in `~/.kube/config` (%s). Open the **Overview** tab and click **⚡ Connect** or **🚀 Install kube-agents**.", len(names), strings.Join(names, ", "))
				}
			}
			newMessages = append(newMessages, SwarmBoardMessage{
				Seq:   seq,
				Agent: swarmMemberNarrator,
				Text:  msgText,
				Time:  nowISO,
			})
			head = seq
		}
	}

	view := buildLocalSwarmOverview(input, members, head, now)
	viewHTML := buildSwarmOverviewHTML(view.KubeAgents)

	return SwarmSnapshot{
		Title:       title,
		Members:     members,
		NewMessages: newMessages,
		View:        view,
		ViewHTML:    viewHTML,
	}, ledger
}

// PruneStaleKubeAgentsSwarms ensures that across brainDir, only activeRootID owns a
// kube-agents-managed .system_generated/swarm roster.
func PruneStaleKubeAgentsSwarms(brainDir string, activeRootID string, activeMembers map[string]SwarmMember) {
	bDir := strings.TrimSpace(brainDir)
	rootID := strings.TrimSpace(activeRootID)
	if bDir == "" || rootID == "" || rootID == defaultDisconnectedSwarmID {
		return
	}
	entries, err := os.ReadDir(bDir)
	if err != nil {
		return
	}
	claimedConvs := make(map[string]bool, len(activeMembers))
	for _, m := range activeMembers {
		cid := strings.TrimSpace(m.ConversationID)
		if cid != "" && cid != rootID {
			claimedConvs[cid] = true
		}
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		otherID := entry.Name()
		if otherID == "" || otherID == rootID || otherID == defaultDisconnectedSwarmID {
			continue
		}
		otherSwarmDir := filepath.Join(bDir, otherID, systemGeneratedDirName, swarmDirName)
		otherMembersPath := filepath.Join(otherSwarmDir, swarmMembersFileName)
		if !hasKubeAgentsManagedMembers(otherMembersPath) {
			continue
		}
		unmanaged := readExistingUnmanagedMembers(otherMembersPath)
		for k, m := range unmanaged {
			if claimedConvs[strings.TrimSpace(m.ConversationID)] {
				delete(unmanaged, k)
			}
		}
		otherAlive := IsAGYConversationAlive(bDir, otherID)
		if len(unmanaged) == 0 || !otherAlive {
			_ = os.RemoveAll(otherSwarmDir)
			if !otherAlive {
				_ = os.RemoveAll(filepath.Join(bDir, otherID, systemGeneratedDirName, subagentsDirName))
				_ = os.Remove(filepath.Join(bDir, otherID, systemGeneratedDirName))
				_ = os.Remove(filepath.Join(bDir, otherID))
			}
			continue
		}
		rosterDoc := SwarmRosterFile{
			Members: unmanaged,
		}
		if raw, mErr := json.MarshalIndent(rosterDoc, "", "  "); mErr == nil {
			_ = writeFileAtomic(otherMembersPath, raw)
		}
	}
}

func hasKubeAgentsManagedMembers(path string) bool {
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return false
	}
	var wrapped SwarmRosterFile
	if json.Unmarshal(raw, &wrapped) == nil && len(wrapped.Members) > 0 {
		for _, m := range wrapped.Members {
			if m.ManagedBy == swarmManagedByTag {
				return true
			}
		}
		return false
	}
	var existing map[string]SwarmMember
	if json.Unmarshal(raw, &existing) != nil {
		return false
	}
	for _, m := range existing {
		if m.ManagedBy == swarmManagedByTag {
			return true
		}
	}
	return false
}

func readBoardLedger(path string) BoardLedger {
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return BoardLedger{SchemaVersion: swarmLedgerSchema, NextSeq: 1, EventSeqs: make(map[string]int)}
	}
	var l BoardLedger
	if json.Unmarshal(raw, &l) != nil {
		return BoardLedger{SchemaVersion: swarmLedgerSchema, NextSeq: 1, EventSeqs: make(map[string]int)}
	}
	if l.EventSeqs == nil {
		l.EventSeqs = make(map[string]int)
	}
	if l.Authors == nil {
		l.Authors = make(map[string]bool)
	}
	if l.NextSeq <= 0 {
		l.NextSeq = 1
	}
	return l
}

func readExistingBoardLines(path string) ([]string, int) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0
	}
	defer f.Close()
	var lines []string
	maxSeq := 0
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 64*1024), 2*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var hdr struct {
			Seq   int    `json:"seq"`
			Agent string `json:"agent"`
		}
		if json.Unmarshal([]byte(line), &hdr) != nil || strings.TrimSpace(hdr.Agent) == "" {
			continue
		}
		lines = append(lines, line)
		if hdr.Seq > maxSeq {
			maxSeq = hdr.Seq
		}
	}
	return lines, maxSeq
}

func boardHasOrphanedManagedAuthor(lines []string, prevLedger BoardLedger, members map[string]SwarmMember) bool {
	for eventKey := range prevLedger.EventSeqs {
		for _, prefix := range []string{"session-start:", "session-ready:", "session-stop:"} {
			if strings.HasPrefix(eventKey, prefix) {
				memberName := strings.TrimSpace(strings.TrimPrefix(eventKey, prefix))
				if memberName != "" {
					if _, ok := members[memberName]; !ok {
						return true
					}
				}
			}
		}
	}
	if len(prevLedger.Authors) == 0 {
		return false
	}
	for _, line := range lines {
		var hdr struct {
			Agent string `json:"agent"`
		}
		if json.Unmarshal([]byte(line), &hdr) != nil {
			continue
		}
		ag := strings.TrimSpace(hdr.Agent)
		if ag == "" || !prevLedger.Authors[ag] {
			continue
		}
		if _, ok := members[ag]; !ok {
			return true
		}
	}
	return false
}

func readExistingUnmanagedMembers(path string) map[string]SwarmMember {
	out := make(map[string]SwarmMember)
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return out
	}
	var wrapped SwarmRosterFile
	if json.Unmarshal(raw, &wrapped) == nil && len(wrapped.Members) > 0 {
		for k, m := range wrapped.Members {
			if m.ManagedBy == swarmManagedByTag {
				continue
			}
			out[k] = m
		}
		return out
	}
	var existing map[string]SwarmMember
	if json.Unmarshal(raw, &existing) != nil {
		return out
	}
	for k, m := range existing {
		if m.Name == "" || m.ManagedBy == swarmManagedByTag {
			continue
		}
		out[k] = m
	}
	return out
}

func deriveSessionMemberName(conv ConversationEntry, lin ConversationLineage) string {
	for _, candidate := range []string{conv.Alias, lin.SubagentRole, conv.SubagentRole} {
		slug := sanitizeAliasSlug(candidate)
		if slug == "" || slug == defaultConvAlias {
			continue
		}
		if strings.HasPrefix(slug, "session-") || strings.HasPrefix(slug, "s-") {
			return sanitizeMemberName(slug)
		}
		return sanitizeMemberName("session-" + slug)
	}
	if conv.SessionWorkerID != "" {
		return sanitizeMemberName(conv.SessionWorkerID)
	}
	if conv.AGYConversationID != "" {
		short := conv.AGYConversationID
		if len(short) > 8 {
			short = short[:8]
		}
		return sanitizeMemberName("session-" + short)
	}
	return "session-worker"
}

func uniqueMemberName(base string, used map[string]bool) string {
	clean := sanitizeMemberName(base)
	if clean == "" {
		clean = "session-worker"
	}
	if !used[clean] {
		used[clean] = true
		return clean
	}
	for i := 2; i < 100; i++ {
		cand := fmt.Sprintf("%s-%d", clean, i)
		if !used[cand] {
			used[cand] = true
			return cand
		}
	}
	return clean
}

func sanitizeMemberName(raw string) string {
	lower := strings.ToLower(strings.TrimSpace(raw))
	if lower == "" {
		return ""
	}
	var b strings.Builder
	lastDash := false
	for _, r := range lower {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' {
			b.WriteRune(r)
			lastDash = false
		} else if !lastDash && b.Len() > 0 {
			b.WriteByte('-')
			lastDash = true
		}
	}
	return strings.Trim(b.String(), "-")
}
