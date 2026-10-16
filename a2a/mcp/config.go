// Package mcp implements the Model Context Protocol (MCP) server for the
// kube-agents A2A v2 (next) JetStream message bus.
package mcp

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	configDirRelPath        = ".config/kube-agents"
	configFileName          = "mcp-config.json"
	pendingEventsFileName   = "pending-events.json"
	activeConvFileName      = "active_conversation.json"
	prCacheDirRelPath       = ".cache/kube-agents/prs"
	dirPermMode             = 0o700
	filePermMode            = 0o600
	defaultConvAlias        = "default"
	defaultConvTokenPrefix  = "jetski-"
	randomTokenBytes        = 6
	defaultAgentAPIPath     = "/usr/local/google/home/adamparco/.gemini/jetski/bin/agentapi"
	envConfigPath           = "KUBE_AGENTS_MCP_CONFIG"
	envJetskiConversationID = "JETSKI_CONVERSATION_ID"
	envAgentAPIPath         = "JETSKI_AGENTAPI_PATH"
	maxStoredEvents         = 200
	maxConsoleTokenLen      = 64
	tempFileSuffix          = ".tmp"
	defaultConvDescription  = "Default Jetski conversation"
)

var (
	errDeploymentNotFound = errors.New("deployment profile not found")
	errNoDeploymentsSaved = errors.New("no deployments configured or discovered")
)

// DeploymentProfile stores connection and cluster metadata for a discovered
// or manually registered kube-agents deployment.
type DeploymentProfile struct {
	Name              string    `json:"name"`
	KubeContext       string    `json:"kubeContext,omitempty"`
	Namespace         string    `json:"namespace"`
	PlatformAgentName string    `json:"platformAgentName"`
	Mode              string    `json:"mode,omitempty"`
	Version           string    `json:"version,omitempty"`
	ProjectID         string    `json:"projectId,omitempty"`
	ClusterName       string    `json:"clusterName,omitempty"`
	Location          string    `json:"location,omitempty"`
	GitHubRepo        string    `json:"githubRepo,omitempty"`
	ChatPlatform      string    `json:"chatPlatform,omitempty"`
	NATSService       string    `json:"natsService,omitempty"`
	NATSPort          int       `json:"natsPort,omitempty"`
	DirectNATSURL     string    `json:"directNatsUrl,omitempty"`
	CredsObjectName   string    `json:"credsObjectName,omitempty"`
	BusUser           string    `json:"busUser,omitempty"`
	BusAuthValue      string    `json:"busAuthValue,omitempty"`
	ObserverAuthValue string    `json:"observerAuthValue,omitempty"`
	Ready             bool      `json:"ready"`
	StatusMessage     string    `json:"statusMessage,omitempty"`
	AgentProfiles     []string  `json:"agentProfiles,omitempty"`
	LastConnectedAt   time.Time `json:"lastConnectedAt,omitempty"`
	LastDiscoveredAt  time.Time `json:"lastDiscoveredAt,omitempty"`
}

// ConversationEntry tracks a named console conversation token and its mode.
type ConversationEntry struct {
	Alias       string    `json:"alias"`
	Token       string    `json:"token"`
	SessionMode bool      `json:"sessionMode"`
	Description string    `json:"description,omitempty"`
	CreatedAt   time.Time `json:"createdAt"`
	LastUsedAt  time.Time `json:"lastUsedAt"`
}

// PushConfig configures bi-directional event push into Jetski.
type PushConfig struct {
	Enabled              bool      `json:"enabled"`
	JetskiConversationID string    `json:"jetskiConversationId,omitempty"`
	AgentAPIPath         string    `json:"agentApiPath,omitempty"`
	UpdatedAt            time.Time `json:"updatedAt,omitempty"`
}

// PersistentConfig is the on-disk schema stored in ~/.config/kube-agents/mcp-config.json.
type PersistentConfig struct {
	ActiveDeployment   string                       `json:"activeDeployment,omitempty"`
	ActiveConversation string                       `json:"activeConversation,omitempty"`
	DebugMode          bool                         `json:"debugMode,omitempty"`
	Deployments        map[string]DeploymentProfile `json:"deployments"`
	Conversations      map[string]ConversationEntry `json:"conversations"`
	Push               PushConfig                   `json:"push"`
}

// ActiveConversationState is written by Jetski hooks or MCP tools to
// ~/.config/kube-agents/active_conversation.json so the MCP server knows
// which Jetski conversation ID to wake via agentapi.
type ActiveConversationState struct {
	JetskiConversationID string    `json:"jetskiConversationId"`
	UpdatedAt            time.Time `json:"updatedAt"`
}

// PushEvent represents an asynchronous message, notification, or task transition
// received from the kube-agents bus.
type PushEvent struct {
	ID           string          `json:"id"`
	Kind         string          `json:"kind"`
	Deployment   string          `json:"deployment,omitempty"`
	Conversation string          `json:"conversation,omitempty"`
	TaskID       string          `json:"taskId,omitempty"`
	ContextID    string          `json:"contextId,omitempty"`
	Subject      string          `json:"subject,omitempty"`
	Title        string          `json:"title"`
	Summary      string          `json:"summary"`
	Details      string          `json:"details,omitempty"`
	PRLinks      []string        `json:"prLinks,omitempty"`
	Raw          json.RawMessage `json:"raw,omitempty"`
	Timestamp    time.Time       `json:"timestamp"`
	Delivered    bool            `json:"delivered"`
}

// ConfigStore manages thread-safe access and atomic persistence for
// PersistentConfig and pending push events.
type ConfigStore struct {
	mu         sync.Mutex
	path       string
	eventsPath string
	convPath   string
	cfg        PersistentConfig
}

// NewConfigStore loads or initializes a ConfigStore at configPath.
// If configPath is empty, it uses KUBE_AGENTS_MCP_CONFIG or ~/.config/kube-agents/mcp-config.json.
func NewConfigStore(configPath string) (*ConfigStore, error) {
	if configPath == "" {
		configPath = os.Getenv(envConfigPath)
	}
	if configPath == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, fmt.Errorf("resolving user home dir: %w", err)
		}
		configPath = filepath.Join(home, configDirRelPath, configFileName)
	}
	baseDir := filepath.Dir(configPath)
	if err := os.MkdirAll(baseDir, dirPermMode); err != nil {
		return nil, fmt.Errorf("creating config dir %s: %w", baseDir, err)
	}
	s := &ConfigStore{
		path:       configPath,
		eventsPath: filepath.Join(baseDir, pendingEventsFileName),
		convPath:   filepath.Join(baseDir, activeConvFileName),
	}
	if err := s.loadLocked(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *ConfigStore) loadLocked() error {
	raw, err := os.ReadFile(s.path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("reading config %s: %w", s.path, err)
	}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &s.cfg); err != nil {
			return fmt.Errorf("parsing config %s: %w", s.path, err)
		}
	}
	if s.cfg.Deployments == nil {
		s.cfg.Deployments = make(map[string]DeploymentProfile)
	}
	if s.cfg.Conversations == nil {
		s.cfg.Conversations = make(map[string]ConversationEntry)
	}
	if len(s.cfg.Conversations) == 0 {
		token := mintConversationToken()
		now := time.Now().UTC()
		s.cfg.Conversations[defaultConvAlias] = ConversationEntry{
			Alias:       defaultConvAlias,
			Token:       token,
			Description: defaultConvDescription,
			CreatedAt:   now,
			LastUsedAt:  now,
		}
		s.cfg.ActiveConversation = defaultConvAlias
	}
	if s.cfg.ActiveConversation == "" {
		for alias := range s.cfg.Conversations {
			s.cfg.ActiveConversation = alias
			break
		}
	}
	if s.cfg.Push.AgentAPIPath == "" {
		if envPath := strings.TrimSpace(os.Getenv(envAgentAPIPath)); envPath != "" {
			s.cfg.Push.AgentAPIPath = envPath
		} else {
			s.cfg.Push.AgentAPIPath = defaultAgentAPIPath
		}
	}
	if envConv := strings.TrimSpace(os.Getenv(envJetskiConversationID)); envConv != "" && s.cfg.Push.JetskiConversationID == "" {
		s.cfg.Push.JetskiConversationID = envConv
		s.cfg.Push.Enabled = true
	}
	return s.saveLocked()
}

func (s *ConfigStore) saveLocked() error {
	data, err := json.MarshalIndent(s.cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshaling config: %w", err)
	}
	return writeFileAtomic(s.path, data)
}

func writeFileAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, dirPermMode); err != nil {
		return err
	}
	tmp := path + tempFileSuffix
	if err := os.WriteFile(tmp, data, filePermMode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func mintConversationToken() string {
	buf := make([]byte, randomTokenBytes)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%s%d", defaultConvTokenPrefix, time.Now().UnixNano())
	}
	return defaultConvTokenPrefix + hex.EncodeToString(buf)
}

// Path returns the filesystem path of the persistent configuration file.
func (s *ConfigStore) Path() string {
	return s.path
}

// EventsPath returns the filesystem path of the pending push events file.
func (s *ConfigStore) EventsPath() string {
	return s.eventsPath
}

// Snapshot returns a deep copy of the current configuration.
func (s *ConfigStore) Snapshot() PersistentConfig {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := s.cfg
	out.Deployments = make(map[string]DeploymentProfile, len(s.cfg.Deployments))
	for k, v := range s.cfg.Deployments {
		vCopy := v
		vCopy.AgentProfiles = append([]string(nil), v.AgentProfiles...)
		out.Deployments[k] = vCopy
	}
	out.Conversations = make(map[string]ConversationEntry, len(s.cfg.Conversations))
	for k, v := range s.cfg.Conversations {
		out.Conversations[k] = v
	}
	return out
}

// UpsertDeployments merges discovered or configured deployments into the store,
// preserving existing cached credentials if the new profile does not overwrite them.
func (s *ConfigStore) UpsertDeployments(profiles []DeploymentProfile) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, p := range profiles {
		if p.Name == "" {
			continue
		}
		if existing, ok := s.cfg.Deployments[p.Name]; ok {
			if p.BusAuthValue == "" {
				p.BusAuthValue = existing.BusAuthValue
			}
			if p.ObserverAuthValue == "" {
				p.ObserverAuthValue = existing.ObserverAuthValue
			}
			if p.BusUser == "" {
				p.BusUser = existing.BusUser
			}
			if p.DirectNATSURL == "" {
				p.DirectNATSURL = existing.DirectNATSURL
			}
			if p.LastConnectedAt.IsZero() {
				p.LastConnectedAt = existing.LastConnectedAt
			}
		}
		s.cfg.Deployments[p.Name] = p
		if s.cfg.ActiveDeployment == "" && p.Ready {
			s.cfg.ActiveDeployment = p.Name
		}
	}
	if s.cfg.ActiveDeployment == "" && len(s.cfg.Deployments) > 0 {
		names := make([]string, 0, len(s.cfg.Deployments))
		for k := range s.cfg.Deployments {
			names = append(names, k)
		}
		sort.Strings(names)
		s.cfg.ActiveDeployment = names[0]
	}
	return s.saveLocked()
}

// SetActiveDeployment selects a deployment by name, kubeContext, or clusterName
// and marks it as the active deployment.
func (s *ConfigStore) SetActiveDeployment(selector string) (DeploymentProfile, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if selector == "" {
		selector = s.cfg.ActiveDeployment
	}
	if selector == "" && len(s.cfg.Deployments) == 0 {
		return DeploymentProfile{}, errNoDeploymentsSaved
	}
	if p, ok := s.cfg.Deployments[selector]; ok {
		p.LastConnectedAt = time.Now().UTC()
		s.cfg.Deployments[selector] = p
		s.cfg.ActiveDeployment = selector
		_ = s.saveLocked()
		return p, nil
	}
	for name, p := range s.cfg.Deployments {
		if p.KubeContext == selector || p.ClusterName == selector || p.PlatformAgentName == selector || p.ProjectID == selector {
			p.LastConnectedAt = time.Now().UTC()
			s.cfg.Deployments[name] = p
			s.cfg.ActiveDeployment = name
			_ = s.saveLocked()
			return p, nil
		}
	}
	lowerSel := strings.ToLower(strings.TrimSpace(selector))
	if lowerSel != "" {
		for name, p := range s.cfg.Deployments {
			if strings.Contains(strings.ToLower(name), lowerSel) ||
				strings.Contains(strings.ToLower(p.KubeContext), lowerSel) ||
				strings.Contains(strings.ToLower(p.ClusterName), lowerSel) ||
				strings.Contains(strings.ToLower(p.ProjectID), lowerSel) ||
				strings.Contains(strings.ToLower(p.PlatformAgentName), lowerSel) ||
				strings.Contains(strings.ToLower(p.GitHubRepo), lowerSel) {
				p.LastConnectedAt = time.Now().UTC()
				s.cfg.Deployments[name] = p
				s.cfg.ActiveDeployment = name
				_ = s.saveLocked()
				return p, nil
			}
		}
	}
	return DeploymentProfile{}, fmt.Errorf("%w: %q", errDeploymentNotFound, selector)
}

// ActiveDeployment returns the currently selected DeploymentProfile, if any.
func (s *ConfigStore) ActiveDeployment() (DeploymentProfile, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cfg.ActiveDeployment == "" {
		return DeploymentProfile{}, false
	}
	p, ok := s.cfg.Deployments[s.cfg.ActiveDeployment]
	return p, ok
}

// ActiveConversation returns the active ConversationEntry, minting default if needed.
func (s *ConfigStore) ActiveConversation() ConversationEntry {
	s.mu.Lock()
	defer s.mu.Unlock()
	if entry, ok := s.cfg.Conversations[s.cfg.ActiveConversation]; ok {
		return entry
	}
	now := time.Now().UTC()
	entry := ConversationEntry{
		Alias:      defaultConvAlias,
		Token:      mintConversationToken(),
		CreatedAt:  now,
		LastUsedAt: now,
	}
	s.cfg.Conversations[defaultConvAlias] = entry
	s.cfg.ActiveConversation = defaultConvAlias
	_ = s.saveLocked()
	return entry
}

// EnsureConversation creates or switches to a conversation alias (or raw token).
func (s *ConfigStore) EnsureConversation(aliasOrToken string, forceNew bool, description string) (ConversationEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	alias := strings.TrimSpace(aliasOrToken)
	if alias == "" {
		alias = s.cfg.ActiveConversation
		if alias == "" {
			alias = defaultConvAlias
		}
	}
	if !forceNew {
		if existing, ok := s.cfg.Conversations[alias]; ok {
			existing.LastUsedAt = now
			if description != "" {
				existing.Description = description
			}
			s.cfg.Conversations[alias] = existing
			s.cfg.ActiveConversation = alias
			return existing, s.saveLocked()
		}
		for k, existing := range s.cfg.Conversations {
			if existing.Token == alias {
				existing.LastUsedAt = now
				s.cfg.Conversations[k] = existing
				s.cfg.ActiveConversation = k
				return existing, s.saveLocked()
			}
		}
	}
	token := mintConversationToken()
	if !forceNew && isValidConsoleToken(alias) && alias != defaultConvAlias {
		token = alias
	}
	entry := ConversationEntry{
		Alias:       alias,
		Token:       token,
		Description: description,
		CreatedAt:   now,
		LastUsedAt:  now,
	}
	s.cfg.Conversations[alias] = entry
	s.cfg.ActiveConversation = alias
	return entry, s.saveLocked()
}

// SetConversationSessionMode records whether /session mode is active on the conversation.
func (s *ConfigStore) SetConversationSessionMode(aliasOrToken string, sessionMode bool) (ConversationEntry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	target := strings.TrimSpace(aliasOrToken)
	if target == "" {
		target = s.cfg.ActiveConversation
	}
	for alias, entry := range s.cfg.Conversations {
		if alias == target || entry.Token == target {
			entry.SessionMode = sessionMode
			entry.LastUsedAt = time.Now().UTC()
			s.cfg.Conversations[alias] = entry
			return entry, s.saveLocked()
		}
	}
	return ConversationEntry{}, fmt.Errorf("conversation %q not found", target)
}

// DebugMode returns whether kube-agents debug output (tool call tables & timings) is enabled by default.
func (s *ConfigStore) DebugMode() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cfg.DebugMode
}

// SetDebugMode sets whether kube-agents debug output (tool call tables & timings) is enabled by default.
func (s *ConfigStore) SetDebugMode(enabled bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cfg.DebugMode = enabled
	return s.saveLocked()
}

// UpdatePushConfig updates the bi-directional Jetski push configuration.
func (s *ConfigStore) UpdatePushConfig(jetskiConvID string, enabled *bool, agentAPIPath string) (PushConfig, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if strings.TrimSpace(jetskiConvID) != "" {
		s.cfg.Push.JetskiConversationID = strings.TrimSpace(jetskiConvID)
	}
	if enabled != nil {
		s.cfg.Push.Enabled = *enabled
	} else if s.cfg.Push.JetskiConversationID != "" {
		s.cfg.Push.Enabled = true
	}
	if strings.TrimSpace(agentAPIPath) != "" {
		s.cfg.Push.AgentAPIPath = strings.TrimSpace(agentAPIPath)
	}
	s.cfg.Push.UpdatedAt = time.Now().UTC()
	if s.cfg.Push.JetskiConversationID != "" {
		stateData, err := json.MarshalIndent(ActiveConversationState{
			JetskiConversationID: s.cfg.Push.JetskiConversationID,
			UpdatedAt:            s.cfg.Push.UpdatedAt,
		}, "", "  ")
		if err == nil {
			_ = writeFileAtomic(s.convPath, stateData)
		}
	}
	return s.cfg.Push, s.saveLocked()
}

// ResolveJetskiConversationID returns the best known active Jetski conversation ID,
// checking the on-disk active_conversation.json file (updated by hooks), the config store,
// and JETSKI_CONVERSATION_ID environment variable.
func (s *ConfigStore) ResolveJetskiConversationID() string {
	s.mu.Lock()
	convPath := s.convPath
	cfgID := s.cfg.Push.JetskiConversationID
	s.mu.Unlock()

	if raw, err := os.ReadFile(convPath); err == nil && len(raw) > 0 {
		var st ActiveConversationState
		if json.Unmarshal(raw, &st) == nil && strings.TrimSpace(st.JetskiConversationID) != "" {
			return strings.TrimSpace(st.JetskiConversationID)
		}
	}
	if cfgID != "" {
		return cfgID
	}
	return strings.TrimSpace(os.Getenv(envJetskiConversationID))
}

// AppendPendingEvents appends new PushEvents to the on-disk pending-events.json queue.
func (s *ConfigStore) AppendPendingEvents(events ...PushEvent) error {
	if len(events) == 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	existing := s.readPendingEventsLocked()
	seen := make(map[string]bool, len(existing))
	for _, e := range existing {
		seen[e.ID] = true
	}
	for _, e := range events {
		if e.ID != "" && seen[e.ID] {
			continue
		}
		existing = append(existing, e)
		if e.ID != "" {
			seen[e.ID] = true
		}
	}
	if len(existing) > maxStoredEvents {
		existing = existing[len(existing)-maxStoredEvents:]
	}
	return s.writePendingEventsLocked(existing)
}

// DrainPendingEvents returns all undelivered PushEvents and marks them delivered
// (or removes them from the pending file).
func (s *ConfigStore) DrainPendingEvents() ([]PushEvent, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	existing := s.readPendingEventsLocked()
	var undelivered []PushEvent
	for _, e := range existing {
		if !e.Delivered {
			undelivered = append(undelivered, e)
		}
	}
	if len(undelivered) == 0 {
		return nil, nil
	}
	if err := s.writePendingEventsLocked([]PushEvent{}); err != nil {
		return undelivered, err
	}
	return undelivered, nil
}

// PeekPendingEvents returns all undelivered PushEvents without draining them.
func (s *ConfigStore) PeekPendingEvents() []PushEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	existing := s.readPendingEventsLocked()
	var out []PushEvent
	for _, e := range existing {
		if !e.Delivered {
			out = append(out, e)
		}
	}
	return out
}

func (s *ConfigStore) readPendingEventsLocked() []PushEvent {
	raw, err := os.ReadFile(s.eventsPath)
	if err != nil || len(raw) == 0 {
		return nil
	}
	var list []PushEvent
	if err := json.Unmarshal(raw, &list); err != nil {
		return nil
	}
	return list
}

func (s *ConfigStore) writePendingEventsLocked(list []PushEvent) error {
	data, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(s.eventsPath, data)
}

func isValidConsoleToken(token string) bool {
	if len(token) == 0 || len(token) > maxConsoleTokenLen {
		return false
	}
	for _, r := range token {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '-' {
			continue
		}
		return false
	}
	return true
}
