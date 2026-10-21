package mcpbridge

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

const (
	envAGYBrainDir          = "KUBE_AGENTS_AGY_BRAIN_DIR"
	defaultAGYBrainRel      = ".gemini/antigravity/brain"
	systemGeneratedDirName  = ".system_generated"
	subagentsDirName        = "subagents"
	maxLineageWalkDepth     = 8
	subagentTypeKubeSession = "kube-session"
)

// ConversationLineage describes an Antigravity conversation's position in the parent/subagent
// hierarchy, discovered structurally from <brainDir>/<parent>/.system_generated/subagents/<id>.json.
type ConversationLineage struct {
	ConversationID       string `json:"conversationId"`
	ParentConversationID string `json:"parentConversationId,omitempty"`
	RootConversationID   string `json:"rootConversationId"`
	SubagentType         string `json:"subagentType,omitempty"`
	SubagentRole         string `json:"subagentRole,omitempty"`
	SubagentState        string `json:"subagentState,omitempty"`
	IsSubagent           bool   `json:"isSubagent"`
}

// IsSessionSubagent reports whether this lineage descriptor represents a dedicated
// Kube-Agents /session subagent, i.e. one Antigravity invoked with the kube-session type.
// A subagent of any other type that later opens a /session is recognised through the
// conversation it binds (SessionMode / SessionWorkerID), never through its role wording.
func (l ConversationLineage) IsSessionSubagent() bool {
	if !l.IsSubagent {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(l.SubagentType), subagentTypeKubeSession)
}

// IsKilled reports whether Antigravity recorded this subagent in SUBAGENT_STATE_KILLED.
func (l ConversationLineage) IsKilled() bool {
	return strings.EqualFold(strings.TrimSpace(l.SubagentState), "SUBAGENT_STATE_KILLED")
}

// LineageResolver resolves an Antigravity conversation UUID to its parent/root coordinator
// conversation and lists any subagents belonging to a root coordinator conversation.
type LineageResolver interface {
	ResolveLineage(conversationID string, artifactsDirHint string) ConversationLineage
	ListSubagents(rootConversationID string, artifactsDirHint string) map[string]ConversationLineage
	BrainDir(artifactsDirHint string) string
}

type agySubagentDescriptorFile struct {
	ConversationID     string `json:"conversationId"`
	SubagentDescriptor struct {
		TypeName string `json:"typeName"`
		Role     string `json:"role"`
	} `json:"subagentDescriptor"`
	State string `json:"state"`
}

type cachedParentDescriptor struct {
	parentID       string
	descriptorPath string
}

// AGYLineageResolver inspects Antigravity's .system_generated/subagents/<id>.json files
// to resolve parent/child subagent relationships without string heuristics.
type AGYLineageResolver struct {
	ExplicitBrainDir string

	mu    sync.Mutex
	cache map[string]cachedParentDescriptor
}

// NewAGYLineageResolver creates a AGYLineageResolver. If explicitBrainDir is empty,
// the brain directory is resolved from KUBE_AGENTS_AGY_BRAIN_DIR, the MCP _meta
// artifacts_dir hint, or ~/.gemini/antigravity/brain.
func NewAGYLineageResolver(explicitBrainDir string) *AGYLineageResolver {
	return &AGYLineageResolver{
		ExplicitBrainDir: strings.TrimSpace(explicitBrainDir),
		cache:            make(map[string]cachedParentDescriptor),
	}
}

// NewLineageResolverForStore creates the resolver every code path that holds a ConfigStore
// must use. A store at the default location belongs to the user's real Antigravity installation and
// resolves the real brain directory; any other store (tests, sandboxes, alternate --config
// paths) is confined to a brain directory beside its own config file, so nothing operating on
// that store can read or prune another installation's conversations.
func NewLineageResolverForStore(store *ConfigStore) *AGYLineageResolver {
	return NewAGYLineageResolver(brainDirForStore(store))
}

// brainDirForStore returns "" (default resolution) for the default config path and
// <config dir>/brain for any other store location.
func brainDirForStore(store *ConfigStore) string {
	if store == nil || strings.TrimSpace(store.Path()) == "" {
		return ""
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return ""
	}
	if filepath.Clean(store.Path()) == filepath.Join(home, configDirRelPath, configFileName) {
		return ""
	}
	return filepath.Join(filepath.Dir(store.Path()), "brain")
}

// BrainDir returns the resolved Antigravity brain directory path.
func (r *AGYLineageResolver) BrainDir(artifactsDirHint string) string {
	if r != nil && r.ExplicitBrainDir != "" {
		return r.ExplicitBrainDir
	}
	if env := strings.TrimSpace(os.Getenv(envAGYBrainDir)); env != "" {
		return env
	}
	hint := strings.TrimSpace(artifactsDirHint)
	if hint != "" {
		cleaned := filepath.Clean(hint)
		parent := filepath.Dir(cleaned)
		if filepath.Base(parent) == "brain" {
			return parent
		}
	}
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		return filepath.Join(home, defaultAGYBrainRel)
	}
	return ""
}

// ResolveLineage resolves conversationID to its immediate parent and root coordinator
// conversation by walking .system_generated/subagents/<id>.json descriptors.
func (r *AGYLineageResolver) ResolveLineage(conversationID string, artifactsDirHint string) ConversationLineage {
	id := strings.TrimSpace(conversationID)
	if id == "" {
		return ConversationLineage{}
	}
	out := ConversationLineage{
		ConversationID:     id,
		RootConversationID: id,
	}
	brainDir := r.BrainDir(artifactsDirHint)
	if brainDir == "" {
		return out
	}

	cur := id
	for depth := 0; depth < maxLineageWalkDepth; depth++ {
		parentID, desc, ok := r.findParentDescriptor(brainDir, cur)
		if !ok || parentID == "" || parentID == cur {
			break
		}
		if depth == 0 {
			out.IsSubagent = true
			out.ParentConversationID = parentID
			out.SubagentType = strings.TrimSpace(desc.SubagentDescriptor.TypeName)
			out.SubagentRole = strings.TrimSpace(desc.SubagentDescriptor.Role)
			out.SubagentState = strings.TrimSpace(desc.State)
		}
		out.RootConversationID = parentID
		cur = parentID
	}
	return out
}

// ListSubagents returns all subagent descriptors registered under rootConversationID.
func (r *AGYLineageResolver) ListSubagents(rootConversationID string, artifactsDirHint string) map[string]ConversationLineage {
	rootID := strings.TrimSpace(rootConversationID)
	if rootID == "" {
		return nil
	}
	brainDir := r.BrainDir(artifactsDirHint)
	if brainDir == "" {
		return nil
	}
	subDir := filepath.Join(brainDir, rootID, systemGeneratedDirName, subagentsDirName)
	entries, err := os.ReadDir(subDir)
	if err != nil {
		return nil
	}
	out := make(map[string]ConversationLineage)
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		subID := strings.TrimSuffix(entry.Name(), ".json")
		if subID == "" {
			continue
		}
		descPath := filepath.Join(subDir, entry.Name())
		desc, ok := readSubagentDescriptorFile(descPath)
		if !ok {
			continue
		}
		if desc.ConversationID != "" {
			subID = strings.TrimSpace(desc.ConversationID)
		}
		if !IsAGYConversationAlive(brainDir, subID) {
			_ = os.Remove(descPath)
			r.mu.Lock()
			delete(r.cache, subID)
			r.mu.Unlock()
			continue
		}
		r.mu.Lock()
		if r.cache == nil {
			r.cache = make(map[string]cachedParentDescriptor)
		}
		r.cache[subID] = cachedParentDescriptor{
			parentID:       rootID,
			descriptorPath: descPath,
		}
		r.mu.Unlock()

		out[subID] = ConversationLineage{
			ConversationID:       subID,
			ParentConversationID: rootID,
			RootConversationID:   rootID,
			SubagentType:         strings.TrimSpace(desc.SubagentDescriptor.TypeName),
			SubagentRole:         strings.TrimSpace(desc.SubagentDescriptor.Role),
			SubagentState:        strings.TrimSpace(desc.State),
			IsSubagent:           true,
		}
	}
	return out
}

func (r *AGYLineageResolver) findParentDescriptor(brainDir, childID string) (string, agySubagentDescriptorFile, bool) {
	r.mu.Lock()
	cached, hasCached := r.cache[childID]
	r.mu.Unlock()
	if hasCached && cached.descriptorPath != "" && IsAGYConversationAlive(brainDir, cached.parentID) {
		if desc, ok := readSubagentDescriptorFile(cached.descriptorPath); ok {
			return cached.parentID, desc, true
		}
	}

	entries, err := os.ReadDir(brainDir)
	if err != nil {
		return "", agySubagentDescriptorFile{}, false
	}
	targetFile := childID + ".json"
	var fallbackParentID string
	var fallbackPath string
	var fallbackDesc agySubagentDescriptorFile

	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		parentID := entry.Name()
		if parentID == "" || parentID == childID {
			continue
		}
		candidatePath := filepath.Join(brainDir, parentID, systemGeneratedDirName, subagentsDirName, targetFile)
		desc, ok := readSubagentDescriptorFile(candidatePath)
		if !ok {
			continue
		}
		if !IsAGYConversationAlive(brainDir, parentID) {
			if fallbackParentID == "" {
				fallbackParentID = parentID
				fallbackPath = candidatePath
				fallbackDesc = desc
			}
			continue
		}
		r.mu.Lock()
		if r.cache == nil {
			r.cache = make(map[string]cachedParentDescriptor)
		}
		r.cache[childID] = cachedParentDescriptor{
			parentID:       parentID,
			descriptorPath: candidatePath,
		}
		r.mu.Unlock()
		return parentID, desc, true
	}
	if fallbackParentID != "" {
		r.mu.Lock()
		if r.cache == nil {
			r.cache = make(map[string]cachedParentDescriptor)
		}
		r.cache[childID] = cachedParentDescriptor{
			parentID:       fallbackParentID,
			descriptorPath: fallbackPath,
		}
		r.mu.Unlock()
		return fallbackParentID, fallbackDesc, true
	}
	return "", agySubagentDescriptorFile{}, false
}

// IsAGYConversationAlive reports whether conversationID exists in Antigravity's
// sibling conversations store (<brainDir>/../conversations/<id>.db or .pb).
// When the sibling conversations directory does not exist (such as in isolated
// unit tests that only construct a temporary brain directory), it returns true
// unless brainDir or conversationID is empty.
func IsAGYConversationAlive(brainDir string, conversationID string) bool {
	id := strings.TrimSpace(conversationID)
	if id == "" {
		return false
	}
	bDir := strings.TrimSpace(brainDir)
	if bDir == "" {
		return true
	}
	convDir := filepath.Join(filepath.Dir(filepath.Clean(bDir)), "conversations")
	info, err := os.Stat(convDir)
	if err != nil || !info.IsDir() {
		return true
	}
	for _, ext := range []string{".db", ".pb"} {
		if st, err := os.Stat(filepath.Join(convDir, id+ext)); err == nil && !st.IsDir() {
			return true
		}
	}
	return false
}

// AdoptSubagent registers subagentID under newParentID's .system_generated/subagents/
// directory, removes any stale subagent descriptor under other parents, and removes
// any split-brain .system_generated/swarm directory inside the subagent's own brain dir.
func (r *AGYLineageResolver) AdoptSubagent(brainDir, newParentID, subagentID, role string) error {
	bDir := strings.TrimSpace(brainDir)
	if bDir == "" && r != nil {
		bDir = r.BrainDir("")
	}
	parentID := strings.TrimSpace(newParentID)
	subID := strings.TrimSpace(subagentID)
	if bDir == "" || parentID == "" || subID == "" || parentID == subID {
		return nil
	}

	targetSubDir := filepath.Join(bDir, parentID, systemGeneratedDirName, subagentsDirName)
	targetPath := filepath.Join(targetSubDir, subID+".json")

	desc := agySubagentDescriptorFile{
		ConversationID: subID,
		State:          "SUBAGENT_STATE_WAITING",
	}
	desc.SubagentDescriptor.TypeName = subagentTypeKubeSession
	desc.SubagentDescriptor.Role = firstNonEmptyStr(strings.TrimSpace(role), "Session Worker")

	_, hasTarget := false, false
	if existing, ok := readSubagentDescriptorFile(targetPath); ok {
		hasTarget = true
		if existing.SubagentDescriptor.TypeName != "" {
			desc.SubagentDescriptor.TypeName = existing.SubagentDescriptor.TypeName
		}
		if strings.TrimSpace(role) == "" && existing.SubagentDescriptor.Role != "" {
			desc.SubagentDescriptor.Role = existing.SubagentDescriptor.Role
		}
		if existing.State != "" {
			desc.State = existing.State
		}
	}

	alreadyAdoptedHere := false
	if r != nil && hasTarget {
		r.mu.Lock()
		if cached, ok := r.cache[subID]; ok && cached.parentID == parentID {
			alreadyAdoptedHere = true
		}
		r.mu.Unlock()
	}

	if !alreadyAdoptedHere {
		if entries, err := os.ReadDir(bDir); err == nil {
			for _, entry := range entries {
				if !entry.IsDir() {
					continue
				}
				otherParent := entry.Name()
				if otherParent == "" || otherParent == parentID || otherParent == subID {
					continue
				}
				otherPath := filepath.Join(bDir, otherParent, systemGeneratedDirName, subagentsDirName, subID+".json")
				if otherDesc, ok := readSubagentDescriptorFile(otherPath); ok {
					if strings.TrimSpace(role) == "" && otherDesc.SubagentDescriptor.Role != "" {
						desc.SubagentDescriptor.Role = otherDesc.SubagentDescriptor.Role
					}
					if otherDesc.State != "" {
						desc.State = otherDesc.State
					}
					_ = os.Remove(otherPath)
				}
			}
		}
		_ = os.RemoveAll(filepath.Join(bDir, subID, systemGeneratedDirName, swarmDirName))
	}

	raw, err := json.MarshalIndent(desc, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	if existingBytes, err := os.ReadFile(targetPath); err != nil || !bytes.Equal(existingBytes, raw) {
		if err := os.MkdirAll(targetSubDir, 0o755); err != nil {
			return err
		}
		if err := writeFileAtomic(targetPath, raw); err != nil {
			return err
		}
	}

	if r != nil {
		r.mu.Lock()
		if r.cache == nil {
			r.cache = make(map[string]cachedParentDescriptor)
		}
		r.cache[subID] = cachedParentDescriptor{
			parentID:       parentID,
			descriptorPath: targetPath,
		}
		r.mu.Unlock()
	}
	return nil
}

// RemoveSubagent deletes all .system_generated/subagents/<subagentID>.json descriptors across
// brainDir, removes any split-brain swarm directory under subagentID, and evicts subagentID
// from the resolver cache. Returns true if any descriptor file was removed.
func (r *AGYLineageResolver) RemoveSubagent(brainDir, subagentID string) bool {
	bDir := strings.TrimSpace(brainDir)
	if bDir == "" && r != nil {
		bDir = r.BrainDir("")
	}
	subID := strings.TrimSpace(subagentID)
	if subID == "" {
		return false
	}
	if r != nil {
		r.mu.Lock()
		delete(r.cache, subID)
		r.mu.Unlock()
	}
	return removeSubagentDescriptorsOnDisk(bDir, subID)
}

// RemoveSubagentByTarget searches all .system_generated/subagents/*.json descriptors under
// brainDir for a subagent matching target (by conversation UUID, role, or derived Swarm member
// name such as "session-test"), removes the matching descriptor(s), and returns the matched
// ConversationLineage.
func (r *AGYLineageResolver) RemoveSubagentByTarget(brainDir, target string) (ConversationLineage, bool) {
	bDir := strings.TrimSpace(brainDir)
	if bDir == "" && r != nil {
		bDir = r.BrainDir("")
	}
	rawTarget := strings.TrimPrefix(strings.TrimSpace(target), consoleConvPrefix)
	if bDir == "" || rawTarget == "" {
		return ConversationLineage{}, false
	}
	normTarget := sanitizeMemberName(rawTarget)
	normNoPrefix := strings.TrimPrefix(normTarget, "session-")

	entries, err := os.ReadDir(bDir)
	if err != nil {
		return ConversationLineage{}, false
	}
	var matched ConversationLineage
	found := false
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		parentID := entry.Name()
		if parentID == "" {
			continue
		}
		subDir := filepath.Join(bDir, parentID, systemGeneratedDirName, subagentsDirName)
		subFiles, err := os.ReadDir(subDir)
		if err != nil {
			continue
		}
		for _, sf := range subFiles {
			if sf.IsDir() || !strings.HasSuffix(sf.Name(), ".json") {
				continue
			}
			descPath := filepath.Join(subDir, sf.Name())
			desc, ok := readSubagentDescriptorFile(descPath)
			if !ok {
				continue
			}
			subID := firstNonEmptyStr(strings.TrimSpace(desc.ConversationID), strings.TrimSuffix(sf.Name(), ".json"))
			role := strings.TrimSpace(desc.SubagentDescriptor.Role)
			lin := ConversationLineage{
				ConversationID:       subID,
				ParentConversationID: parentID,
				RootConversationID:   parentID,
				SubagentType:         strings.TrimSpace(desc.SubagentDescriptor.TypeName),
				SubagentRole:         role,
				SubagentState:        strings.TrimSpace(desc.State),
				IsSubagent:           true,
			}
			memberName := deriveSessionMemberName(ConversationEntry{Alias: role, AGYConversationID: subID}, lin)
			roleSlug := sanitizeMemberName(role)
			if strings.EqualFold(subID, rawTarget) ||
				strings.EqualFold(role, rawTarget) ||
				(normTarget != "" && (memberName == normTarget || roleSlug == normTarget || "session-"+roleSlug == normTarget)) ||
				(normNoPrefix != "" && (roleSlug == normNoPrefix || memberName == "session-"+normNoPrefix)) {
				_ = os.Remove(descPath)
				_ = os.RemoveAll(filepath.Join(bDir, subID, systemGeneratedDirName, swarmDirName))
				if r != nil {
					r.mu.Lock()
					delete(r.cache, subID)
					r.mu.Unlock()
				}
				if !found {
					matched = lin
					found = true
				}
			}
		}
	}
	return matched, found
}

func removeSubagentDescriptorsOnDisk(brainDir, subagentID string) bool {
	bDir := strings.TrimSpace(brainDir)
	subID := strings.TrimSpace(subagentID)
	if bDir == "" || subID == "" {
		return false
	}
	entries, err := os.ReadDir(bDir)
	if err != nil {
		return false
	}
	removed := false
	targetFile := subID + ".json"
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		parentID := entry.Name()
		if parentID == "" {
			continue
		}
		p := filepath.Join(bDir, parentID, systemGeneratedDirName, subagentsDirName, targetFile)
		if err := os.Remove(p); err == nil {
			removed = true
		}
	}
	_ = os.RemoveAll(filepath.Join(bDir, subID, systemGeneratedDirName, swarmDirName))
	return removed
}

func readSubagentDescriptorFile(path string) (agySubagentDescriptorFile, bool) {
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) == 0 {
		return agySubagentDescriptorFile{}, false
	}
	var desc agySubagentDescriptorFile
	if err := json.Unmarshal(raw, &desc); err != nil {
		return agySubagentDescriptorFile{}, false
	}
	return desc, true
}
