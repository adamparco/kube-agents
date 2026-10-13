package gateway

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// PrincipalMap resolves backend-native user ids to principals in our trust
// domain. For Discord it is a mounted install-side ConfigMap (nothing is
// checked into the repo) and a test fixture by construction: a Discord
// identity never maps to a real cloud principal, full stop. For Slack it is
// the admin-owned a2a-slack-principal-map Secret, joining the immutable
// user_id to an IdP-sourced principal (spec-chatops-gateway.md, "The Slack
// adapter"). A Discord sender with no entry cannot be verified and their
// message is dropped at ingress; on Slack the map is an override, and a
// listed sender with no entry is attributed by member id.
type PrincipalMap struct {
	mu sync.RWMutex
	m  map[string]string
}

// LoadPrincipalMap reads the map from a directory of files (a mounted
// ConfigMap or Secret: one file per backend user id, content is the
// principal) or from a single file of "id principal" lines. A missing path
// yields an empty map and the gateway runs: on Discord every message then
// drops at verification, the honest failure for an install without its test
// table, and on Slack every listed sender is attributed by member id.
func LoadPrincipalMap(path string) (*PrincipalMap, error) {
	pm := &PrincipalMap{m: map[string]string{}}
	info, err := os.Stat(path)
	if os.IsNotExist(err) {
		return pm, nil
	}
	if err != nil {
		return nil, fmt.Errorf("principal map %s: %w", path, err)
	}
	if !info.IsDir() {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("principal map %s: %w", path, err)
		}
		for _, line := range strings.Split(string(data), "\n") {
			fields := strings.Fields(line)
			if len(fields) == 2 && !strings.HasPrefix(fields[0], "#") {
				pm.m[fields[0]] = fields[1]
			}
		}
		return pm, nil
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, fmt.Errorf("principal map %s: %w", path, err)
	}
	for _, e := range entries {
		// Kubelet renders ConfigMap volumes with ..data/..timestamp symlink
		// machinery; only plain keys are entries.
		if strings.HasPrefix(e.Name(), ".") || e.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(path, e.Name()))
		if err != nil {
			return nil, fmt.Errorf("principal map entry %s: %w", e.Name(), err)
		}
		pm.m[e.Name()] = strings.TrimSpace(string(data))
	}
	return pm, nil
}

// Resolve returns the principal for a backend user id, or "" if the id is
// unmapped.
func (p *PrincipalMap) Resolve(userID string) string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.m[userID]
}

// Section returns the entries under keyPrefix as a map of their own, with
// the prefix removed from every key and only values under valuePrefix kept.
// It is how the inject door's prefixed map is read by the code that resolves
// a roster's raw author ids (BuildAuthority, hashRoster), so the requester
// hashes to the same principal there as in the authority block's requester
// field. The refusal of a value outside valuePrefix is the same refusal
// Gateway.resolveInjectPrincipal makes; here it is a drop, there it is a
// logged error, and neither honours the entry.
func (p *PrincipalMap) Section(keyPrefix, valuePrefix string) *PrincipalMap {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := &PrincipalMap{m: map[string]string{}}
	for k, v := range p.m {
		if strings.HasPrefix(k, keyPrefix) && strings.HasPrefix(v, valuePrefix) {
			out.m[strings.TrimPrefix(k, keyPrefix)] = v
		}
	}
	return out
}

// Len reports how many identities are mapped.
func (p *PrincipalMap) Len() int {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.m)
}

// Pseudonymizer HMACs identifiers with the install's attribution salt before
// anything is written to the bus — the bus holds labelled content at rest for
// the whole retention window, so identifiers on it get the same treatment as
// the session KV. The plaintext join lives in the gateway's local ingress
// log.
type Pseudonymizer struct {
	salt []byte
}

// NewPseudonymizer builds one from the install's salt.
func NewPseudonymizer(salt []byte) *Pseudonymizer {
	return &Pseudonymizer{salt: salt}
}

// Hash returns the pseudonym for an identifier: "hmac:" plus the first 16
// hex bytes of HMAC-SHA256 under the install salt, matching the shipped
// attribution posture.
func (p *Pseudonymizer) Hash(id string) string {
	if id == "" {
		return ""
	}
	mac := hmac.New(sha256.New, p.salt)
	mac.Write([]byte(id))
	return "hmac:" + hex.EncodeToString(mac.Sum(nil))[:32]
}

const inboundIdentityCap = 4096

// InboundIdentity holds the immutable platform sender ID and canonical event ID
// captured by a chat adapter alongside an InboundMessage.
type InboundIdentity struct {
	Backend  string
	SenderID string
	EventID  string
}

var (
	inboundIdentityMu        sync.RWMutex
	inboundIdentities        = map[string]InboundIdentity{}
	inboundIdentitiesByMsgID = map[string]InboundIdentity{}
	inboundIdentityOrder     []string
	gchatSenderIDByEmail     = map[string]string{}
)

func inboundIdentityKey(msg InboundMessage) string {
	return msg.Conversation + "\x00" + msg.MessageID + "\x00" + msg.AuthorID
}

// WithInboundIdentity records the immutable platform sender ID (e.g. "users/<id>" on
// Google Chat, "U..." on Slack, snowflake on Discord) and canonical event ID for msg.
func WithInboundIdentity(msg InboundMessage, senderID, eventID string) InboundMessage {
	return withInboundIdentity(msg, inferBackend(msg), senderID, eventID)
}

func withInboundIdentity(msg InboundMessage, backend, senderID, eventID string) InboundMessage {
	senderID = strings.TrimSpace(senderID)
	eventID = strings.TrimSpace(eventID)
	backend = strings.TrimSpace(backend)
	if backend == "" {
		backend = inferBackend(msg)
	}
	id := InboundIdentity{
		Backend:  backend,
		SenderID: senderID,
		EventID:  eventID,
	}
	key := inboundIdentityKey(msg)

	inboundIdentityMu.Lock()
	defer inboundIdentityMu.Unlock()

	if _, exists := inboundIdentities[key]; !exists {
		inboundIdentityOrder = append(inboundIdentityOrder, key)
		if len(inboundIdentityOrder) > inboundIdentityCap {
			evict := inboundIdentityOrder[0]
			delete(inboundIdentities, evict)
			inboundIdentityOrder = inboundIdentityOrder[1:]
		}
	}
	inboundIdentities[key] = id
	if msg.MessageID != "" {
		if len(inboundIdentitiesByMsgID) >= inboundIdentityCap {
			inboundIdentitiesByMsgID = map[string]InboundIdentity{}
		}
		inboundIdentitiesByMsgID[msg.MessageID] = id
	}
	if (backend == "gchat" || backend == "googlechat") && strings.HasPrefix(senderID, "users/") && msg.AuthorID != "" {
		if len(gchatSenderIDByEmail) >= inboundIdentityCap {
			gchatSenderIDByEmail = map[string]string{}
		}
		gchatSenderIDByEmail[strings.ToLower(strings.TrimSpace(msg.AuthorID))] = senderID
	}
	return msg
}

func lookupInboundIdentity(msg InboundMessage) (InboundIdentity, bool) {
	inboundIdentityMu.RLock()
	defer inboundIdentityMu.RUnlock()
	if id, ok := inboundIdentities[inboundIdentityKey(msg)]; ok {
		return id, true
	}
	if msg.MessageID != "" {
		if id, ok := inboundIdentitiesByMsgID[msg.MessageID]; ok {
			return id, true
		}
	}
	return InboundIdentity{}, false
}

func inferBackend(msg InboundMessage) string {
	if b := strings.TrimSpace(msg.Backend); b != "" {
		return b
	}
	if id, ok := lookupInboundIdentity(msg); ok && id.Backend != "" {
		return id.Backend
	}
	switch {
	case strings.HasPrefix(msg.Conversation, "slack:"):
		return "slack"
	case strings.HasPrefix(msg.Conversation, "gchat:"), strings.HasPrefix(msg.Conversation, "googlechat:"):
		return "gchat"
	case strings.HasPrefix(msg.Conversation, "discord:"):
		return "discord"
	default:
		return ""
	}
}

// SenderID returns the immutable platform sender identifier associated with m
// ("users/<id>" on Google Chat, "U..." on Slack, snowflake on Discord).
func (m InboundMessage) SenderID() string {
	if id, ok := lookupInboundIdentity(m); ok && id.SenderID != "" {
		return id.SenderID
	}
	backend := inferBackend(m)
	author := strings.TrimSpace(m.AuthorID)
	if backend == "gchat" || backend == "googlechat" {
		if strings.HasPrefix(author, "users/") {
			return author
		}
		inboundIdentityMu.RLock()
		mapped := gchatSenderIDByEmail[strings.ToLower(author)]
		inboundIdentityMu.RUnlock()
		if mapped != "" {
			return mapped
		}
	}
	return author
}

// EventID returns the canonical platform event identifier associated with m
// ("googlechat:<message-name>", "slack:<ts>", "discord:<id>").
func (m InboundMessage) EventID() string {
	if id, ok := lookupInboundIdentity(m); ok && id.EventID != "" {
		return id.EventID
	}
	msgID := strings.TrimSpace(m.MessageID)
	if msgID == "" {
		return ""
	}
	switch inferBackend(m) {
	case "gchat", "googlechat":
		return "googlechat:" + msgID
	case "slack":
		return "slack:" + msgID
	case "discord":
		return "discord:" + msgID
	default:
		if b := inferBackend(m); b != "" {
			return b + ":" + msgID
		}
		return msgID
	}
}

// CanonicalRosterPrincipal returns the immutable 06 §1.2 V-11 principal string
// ("slack:U...", "googlechat:users/...", "discord:...") used by ApprovalRoster
// and ActionRecord.Spec.Requester.ID.
func CanonicalRosterPrincipal(msg InboundMessage) (string, error) {
	backend := inferBackend(msg)
	id := strings.TrimSpace(msg.SenderID())
	if id == "" {
		return "", fmt.Errorf("missing immutable platform sender ID for backend %q", backend)
	}
	switch backend {
	case "slack":
		id = strings.TrimPrefix(id, "slack:")
		if id == "" {
			return "", fmt.Errorf("missing immutable platform sender ID for backend %q", backend)
		}
		return "slack:" + id, nil
	case "gchat", "googlechat":
		id = strings.TrimPrefix(id, "googlechat:")
		id = strings.TrimPrefix(id, "gchat:")
		if !strings.HasPrefix(id, "users/") || len(strings.TrimPrefix(id, "users/")) == 0 {
			return "", fmt.Errorf("googlechat sender ID %q must have form users/<id>", id)
		}
		return "googlechat:" + id, nil
	case "discord":
		id = strings.TrimPrefix(id, "discord:")
		if id == "" {
			return "", fmt.Errorf("missing immutable platform sender ID for backend %q", backend)
		}
		return "discord:" + id, nil
	default:
		return "", fmt.Errorf("unsupported backend %q for roster principal", backend)
	}
}

