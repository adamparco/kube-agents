package broker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"
)

const (
	// DefaultVerifyTimeout bounds the broker's wait for a capability verification reply.
	DefaultVerifyTimeout = 5 * time.Second

	// DefaultBrokerVerifierCaller is the default NATS principal name the broker uses
	// when querying a2a.cap.verify.<caller>.
	DefaultBrokerVerifierCaller = "broker"

	// VerbBrokerPropose is the capability verb required to propose an ActionEnvelope.
	VerbBrokerPropose = "broker.propose"
	// VerbBrokerApprove is the capability verb required to approve or reject a gated ActionRecord.
	VerbBrokerApprove = "broker.approve"

	capVerifyPrefix = "a2a.cap.verify."
	capReplyPrefix  = "a2a.cap.reply."
)

// CapRef pins a capability entry in the NATS KV `cap` bucket by key and revision.
type CapRef struct {
	Key      string `json:"key"`
	Revision uint64 `json:"revision"`
}

// String formats CapRef in the canonical "<key> @<revision>" wire format.
func (r CapRef) String() string {
	return r.Key + " @" + strconv.FormatUint(r.Revision, 10)
}

// UnmarshalJSON accepts either a JSON object `{"key":"...","revision":N}` or a
// JSON string `"<key> @<revision>"` (used by gateway.ActionApprovalMessage).
func (r *CapRef) UnmarshalJSON(data []byte) error {
	trimmed := strings.TrimSpace(string(data))
	if trimmed == "" || trimmed == "null" {
		*r = CapRef{}
		return nil
	}
	if strings.HasPrefix(trimmed, `"`) {
		var s string
		if err := json.Unmarshal(data, &s); err != nil {
			return err
		}
		if strings.TrimSpace(s) == "" {
			*r = CapRef{}
			return nil
		}
		parsed, err := ParseCapRef(s)
		if err != nil {
			return err
		}
		*r = parsed
		return nil
	}
	type rawCapRef CapRef
	var raw rawCapRef
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	*r = CapRef(raw)
	return nil
}

// ParseCapRef parses a capability reference from "<key> @<rev>", "<key>@<rev>",
// "cap:<key>#<rev>", "<key>#<rev>", or JSON `{"key":"...","revision":N}`.
func ParseCapRef(s string) (CapRef, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return CapRef{}, fmt.Errorf("capability reference is empty")
	}
	if strings.HasPrefix(s, "{") {
		var ref CapRef
		if err := json.Unmarshal([]byte(s), &ref); err != nil {
			return CapRef{}, fmt.Errorf("invalid JSON capability reference: %w", err)
		}
		if strings.TrimSpace(ref.Key) == "" || ref.Revision == 0 {
			return CapRef{}, fmt.Errorf("capability reference requires non-empty key and non-zero revision")
		}
		return ref, nil
	}
	trimmed := strings.TrimSpace(strings.TrimPrefix(s, "cap:"))
	if idx := strings.LastIndexAny(trimmed, "@#"); idx > 0 {
		key := strings.TrimSpace(trimmed[:idx])
		revStr := strings.TrimSpace(trimmed[idx+1:])
		if key == "" || revStr == "" {
			return CapRef{}, fmt.Errorf("capability reference %q must have both key and revision", s)
		}
		rev, err := strconv.ParseUint(revStr, 10, 64)
		if err != nil || rev == 0 {
			return CapRef{}, fmt.Errorf("capability reference %q has invalid revision", s)
		}
		return CapRef{Key: key, Revision: rev}, nil
	}
	return CapRef{}, fmt.Errorf("capability reference %q is missing @revision or #revision pin", s)
}

// CapVerifyRequest mirrors capability.Request on the a2a.cap.verify.<caller> subject.
type CapVerifyRequest struct {
	Ref      CapRef `json:"ref"`
	Verb     string `json:"verb"`
	Resource string `json:"resource"`
}

// CapVerifyResponse mirrors capability.Response returned on a2a.cap.reply.<caller>.<nuid>.
type CapVerifyResponse struct {
	Allowed        bool   `json:"allowed"`
	Reason         string `json:"reason,omitempty"`
	Requester      string `json:"requester,omitempty"`
	EnvelopeSHA256 string `json:"envelopeSha256,omitempty"`
}

// CapabilityAssertionVerifier verifies capability references against the NATS
// capability verification service (a2a.cap.verify.<self>).
type CapabilityAssertionVerifier struct {
	NC      *nats.Conn
	Self    string
	Timeout time.Duration
}

// NewCapabilityAssertionVerifier returns a verifier bound to the given NATS connection
// and caller identity (defaults to "broker" when self is empty).
func NewCapabilityAssertionVerifier(nc *nats.Conn, self string) *CapabilityAssertionVerifier {
	if strings.TrimSpace(self) == "" {
		self = DefaultBrokerVerifierCaller
	}
	return &CapabilityAssertionVerifier{
		NC:      nc,
		Self:    self,
		Timeout: DefaultVerifyTimeout,
	}
}

// Verify parses assertion as a CapRef and verifies it for a proposal against resource
// and expectedEnvelopeSHA256.
func (v *CapabilityAssertionVerifier) Verify(
	ctx context.Context,
	assertion string,
	resource string,
	expectedEnvelopeSHA256 string,
) (Requester, error) {
	ref, err := ParseCapRef(assertion)
	if err != nil {
		return Requester{}, newAssertionRefusal("invalid requester capability assertion: " + err.Error())
	}
	return v.VerifyProposal(ctx, ref, resource, expectedEnvelopeSHA256)
}

// VerifyProposal verifies that ref authorizes VerbBrokerPropose on resource and matches
// expectedEnvelopeSHA256 (or expectedEnvelopeSHA256Alt if provided via VerifyEnvelope).
func (v *CapabilityAssertionVerifier) VerifyProposal(
	ctx context.Context,
	ref CapRef,
	resource string,
	expectedEnvelopeSHA256 string,
	altEnvelopeSHA256 ...string,
) (Requester, error) {
	if strings.TrimSpace(ref.Key) == "" || ref.Revision == 0 {
		return Requester{}, newAssertionRefusal("capability reference requires non-empty key and non-zero revision")
	}
	resp, err := v.queryVerifier(ctx, CapVerifyRequest{
		Ref:      ref,
		Verb:     VerbBrokerPropose,
		Resource: resource,
	})
	if err != nil {
		return Requester{}, err
	}
	if !resp.Allowed {
		reason := resp.Reason
		if reason == "" {
			reason = "capability denied by verifier"
		}
		return Requester{}, newAssertionRefusal("capability verification refused: " + reason)
	}
	if strings.TrimSpace(resp.Requester) == "" {
		return Requester{}, newAssertionRefusal("capability chain carries no verified requester principal")
	}
	wantSHA := normalizeSHA256Hex(expectedEnvelopeSHA256)
	if wantSHA != "" {
		gotSHA := normalizeSHA256Hex(resp.EnvelopeSHA256)
		if gotSHA == "" {
			return Requester{}, newAssertionRefusal("capability entry is missing envelopeSha256 binding")
		}
		matched := gotSHA == wantSHA
		if !matched {
			for _, alt := range altEnvelopeSHA256 {
				if a := normalizeSHA256Hex(alt); a != "" && gotSHA == a {
					matched = true
					break
				}
			}
		}
		if !matched {
			return Requester{}, newAssertionRefusal("capability envelopeSha256 does not match submitted ActionEnvelope")
		}
	}
	return Requester{
		Kind:      inferRequesterKind(resp.Requester),
		ID:        resp.Requester,
		Platform:  inferRequesterPlatform(resp.Requester),
		Assertion: ref.String(),
	}, nil
}

// VerifyEnvelope verifies the capability assertion in env.Requester.Assertion against
// rawEnvelopeBytes and resource, overwriting env.Requester with the verifier-resolved
// principal on success.
func (v *CapabilityAssertionVerifier) VerifyEnvelope(
	ctx context.Context,
	env *Envelope,
	rawEnvelopeBytes []byte,
	resource string,
) (Requester, error) {
	if env == nil {
		return Requester{}, newAssertionRefusal("action envelope is nil")
	}
	if strings.TrimSpace(env.Requester.Assertion) == "" {
		return Requester{}, newAssertionRefusal("requester.assertion is required when capability verification is enabled")
	}
	ref, err := ParseCapRef(env.Requester.Assertion)
	if err != nil {
		return Requester{}, newAssertionRefusal("invalid requester.assertion: " + err.Error())
	}
	primarySHA, withoutAssertionSHA, err := EnvelopeDigests(rawEnvelopeBytes)
	if err != nil {
		return Requester{}, newAssertionRefusal("failed to canonicalize envelope for digest check: " + err.Error())
	}
	verifiedReq, err := v.VerifyProposal(ctx, ref, resource, primarySHA, withoutAssertionSHA)
	if err != nil {
		return Requester{}, err
	}
	if env.Requester.DisplayName != "" && verifiedReq.DisplayName == "" {
		verifiedReq.DisplayName = env.Requester.DisplayName
	}
	env.Requester = verifiedReq
	return verifiedReq, nil
}

// VerifyApproval verifies that ref is a root capability authorizing VerbBrokerApprove
// on resource, bound to expectedEnvelopeSHA256, and minted for a principal distinct from
// originalRequesterID (four-eyes separation).
func (v *CapabilityAssertionVerifier) VerifyApproval(
	ctx context.Context,
	ref CapRef,
	resource string,
	expectedEnvelopeSHA256 string,
	originalRequesterID string,
) (string, error) {
	if strings.TrimSpace(ref.Key) == "" || ref.Revision == 0 {
		return "", newAssertionRefusal("approval capability reference requires non-empty key and non-zero revision")
	}
	resp, err := v.queryVerifier(ctx, CapVerifyRequest{
		Ref:      ref,
		Verb:     VerbBrokerApprove,
		Resource: resource,
	})
	if err != nil {
		return "", err
	}
	if !resp.Allowed {
		reason := resp.Reason
		if reason == "" {
			reason = "approval capability denied by verifier"
		}
		return "", newAssertionRefusal("approval capability verification refused: " + reason)
	}
	approver := strings.TrimSpace(resp.Requester)
	if approver == "" {
		return "", newAssertionRefusal("approval capability carries no verified approver principal")
	}
	if strings.TrimSpace(originalRequesterID) != "" && approver == strings.TrimSpace(originalRequesterID) {
		return "", newAssertionRefusal(fmt.Sprintf("four-eyes violation: approver %q matches original requester", approver))
	}
	wantSHA := normalizeSHA256Hex(expectedEnvelopeSHA256)
	gotSHA := normalizeSHA256Hex(resp.EnvelopeSHA256)
	if wantSHA != "" && gotSHA != "" && gotSHA != wantSHA {
		return "", newAssertionRefusal("approval capability envelopeSha256 does not match ActionRecord envelope")
	}
	return approver, nil
}

func (v *CapabilityAssertionVerifier) queryVerifier(ctx context.Context, req CapVerifyRequest) (CapVerifyResponse, error) {
	if v == nil || v.NC == nil {
		return CapVerifyResponse{}, newAssertionRefusal("capability verifier NATS connection is not configured")
	}
	self := strings.TrimSpace(v.Self)
	if self == "" {
		self = DefaultBrokerVerifierCaller
	}
	payload, err := json.Marshal(req)
	if err != nil {
		return CapVerifyResponse{}, newAssertionRefusal("failed to marshal capability verify request: " + err.Error())
	}
	timeout := v.Timeout
	if timeout <= 0 {
		timeout = DefaultVerifyTimeout
	}
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	replySubj := capReplyPrefix + self + "." + nuid.Next()
	sub, err := v.NC.SubscribeSync(replySubj)
	if err != nil {
		return CapVerifyResponse{}, newAssertionRefusal("capability verifier subscribe failed: " + err.Error())
	}
	defer func() { _ = sub.Unsubscribe() }()

	verifySubj := capVerifyPrefix + self
	if err := v.NC.PublishRequest(verifySubj, replySubj, payload); err != nil {
		return CapVerifyResponse{}, newAssertionRefusal("capability verifier publish failed: " + err.Error())
	}
	msg, err := sub.NextMsgWithContext(reqCtx)
	if err != nil {
		return CapVerifyResponse{}, newAssertionRefusal("capability verifier unreachable or timed out: " + err.Error())
	}
	var resp CapVerifyResponse
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		return CapVerifyResponse{}, newAssertionRefusal("capability verifier returned malformed response: " + err.Error())
	}
	return resp, nil
}

// ReasonInvalidRequesterAssertion is the refusal reason returned when a requester or
// approval capability assertion fails verification.
const ReasonInvalidRequesterAssertion = "invalid-requester-assertion"

// EnvelopeSHA256 computes the 64-character lowercase hex SHA-256 digest of the
// RFC 8785 JCS-canonicalized JSON bytes.
func EnvelopeSHA256(rawJSON []byte) (string, error) {
	canon, err := Canonicalize(rawJSON)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canon)
	return hex.EncodeToString(sum[:]), nil
}

// EnvelopeDigests returns the primary JCS SHA-256 of rawJSON and an alternate digest
// computed with requester.assertion omitted (supporting HTTP callers that mint a
// capability before embedding the returned key@revision into requester.assertion).
func EnvelopeDigests(rawJSON []byte) (primary string, withoutAssertion string, err error) {
	canon, err := Canonicalize(rawJSON)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256(canon)
	primary = hex.EncodeToString(sum[:])

	var parsed any
	if err := json.Unmarshal(rawJSON, &parsed); err == nil {
		if m, ok := parsed.(map[string]any); ok {
			if reqRaw, ok := m["requester"].(map[string]any); ok {
				if _, hasAssertion := reqRaw["assertion"]; hasAssertion {
					reqCopy := make(map[string]any, len(reqRaw))
					for k, v := range reqRaw {
						if k != "assertion" {
							reqCopy[k] = v
						}
					}
					mCopy := make(map[string]any, len(m))
					for k, v := range m {
						mCopy[k] = v
					}
					mCopy["requester"] = reqCopy
					if altRaw, mErr := json.Marshal(mCopy); mErr == nil {
						if altCanon, altErr := Canonicalize(altRaw); altErr == nil {
							altSum := sha256.Sum256(altCanon)
							withoutAssertion = hex.EncodeToString(altSum[:])
						}
					}
				}
			}
		}
	}
	return primary, withoutAssertion, nil
}

func normalizeSHA256Hex(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	s = strings.TrimPrefix(s, "sha256:")
	return s
}

func inferRequesterKind(id string) string {
	switch {
	case strings.HasPrefix(id, "slack:"),
		strings.HasPrefix(id, "googlechat:"),
		strings.HasPrefix(id, "discord:"),
		strings.HasPrefix(id, "kubectl:"):
		return "human"
	case strings.HasPrefix(id, "agent:"),
		strings.HasPrefix(id, "session:"):
		return "agent"
	default:
		return "system"
	}
}

func inferRequesterPlatform(id string) string {
	switch {
	case strings.HasPrefix(id, "slack:"):
		return "slack"
	case strings.HasPrefix(id, "googlechat:"):
		return "googlechat"
	case strings.HasPrefix(id, "kubectl:"):
		return "kubectl"
	case strings.HasPrefix(id, "agent:"),
		strings.HasPrefix(id, "session:"):
		return "mesh"
	default:
		return ""
	}
}

func newAssertionRefusal(detail string) *Refusal {
	return &Refusal{
		Status:        http.StatusUnauthorized,
		Reason:        ReasonInvalidRequesterAssertion,
		Detail:        detail,
		Journal:       true,
		SecurityEvent: true,
	}
}
