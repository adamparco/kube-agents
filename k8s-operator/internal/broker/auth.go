/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package broker

import (
	"context"
	"crypto/subtle"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	authnv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// TokenAudience is the audience a caller's projected ServiceAccount token must carry (08 §2.3).
// Not configurable. An audience that could be set per install is an audience an operator can
// widen to `https://kubernetes.default.svc` "to make it work", which is exactly the token
// V-BRK-017 exists to refuse.
const TokenAudience = "kubeagents-broker"

// DefaultTrustDomain is the SPIFFE trust domain of an in-cluster mesh identity.
const DefaultTrustDomain = "cluster.local"

// Identity is who the broker decided is calling, derived entirely from the transport and the
// TokenReview -- never from the request body (03 §4.1 step 1).
//
// Tier and Scope are on this struct rather than read from the envelope because that is the single
// most load-bearing decision in the whole security model: the classifier, the scope check and the
// journal all consume them, and an envelope that could supply either would let an agent classify
// its own action as routine and write outside its namespace. V-BRK-002 is the check that this
// stays true.
type Identity struct {
	// Username is the full `system:serviceaccount:<namespace>:<name>` from the TokenReview.
	Username string
	// Namespace and ServiceAccount are the parsed halves of Username.
	Namespace      string
	ServiceAccount string
	// UID is the token's subject UID. Recorded so a deleted-and-recreated SA of the same name is
	// distinguishable in the journal.
	UID string
	// AgentName, Tier and Scope come from the broker's own configuration, selected by the
	// authenticated caller. They are the agent this broker serves, not a claim the caller made.
	AgentName string
	Tier      agentv1alpha1.AgentTier
	Scope     string
	// PeerSPIFFE is the mesh identity from the client certificate, when one was presented.
	PeerSPIFFE string
}

// ExpectedCaller is the ONE reader identity this broker accepts (08 §2.3).
//
// Singular, not a list. A broker is deployed per agent and serves exactly one; a list would make
// "which agent is this action attributed to?" a lookup with more than one possible answer, and
// the first time two entries shared a namespace the attribution would be a guess.
type ExpectedCaller struct {
	Namespace      string
	ServiceAccount string
	AgentName      string
	Tier           agentv1alpha1.AgentTier
	Scope          string
}

// TokenReviewer submits a TokenReview. An interface rather than a *kubernetes.Clientset so the
// refusal paths can be tested without an API server -- V-BRK-017 in particular needs a review
// response with `authenticated: true` and the WRONG audience, which a real cluster will not
// produce on demand.
type TokenReviewer interface {
	Review(ctx context.Context, token string, audiences []string) (*authnv1.TokenReviewStatus, error)
}

// APITokenReviewer is the production reviewer, backed by the API server.
type APITokenReviewer struct {
	Client kubernetes.Interface
}

// Review submits the TokenReview with the requested audiences.
//
// Passing `Audiences` is not optional politeness: with an empty audience list the API server
// validates the token against the API server's OWN audience, so a default ServiceAccount token
// comes back authenticated and V-BRK-017 fails. The audience must be asserted in the REQUEST and
// re-checked in the response, because a cluster whose API server does not support audience
// validation returns success while silently ignoring the field.
func (r APITokenReviewer) Review(ctx context.Context, token string, audiences []string) (*authnv1.TokenReviewStatus, error) {
	review := &authnv1.TokenReview{
		ObjectMeta: metav1.ObjectMeta{},
		Spec: authnv1.TokenReviewSpec{
			Token:     token,
			Audiences: audiences,
		},
	}
	out, err := r.Client.AuthenticationV1().TokenReviews().Create(ctx, review, metav1.CreateOptions{})
	if err != nil {
		return nil, err
	}
	return &out.Status, nil
}

// Authenticator implements the two-layer check of 08 §2.3: mutual TLS AND a projected token with
// the broker's audience. Both, always. V-BRK-009 asserts that neither alone gets through, which
// is why the two checks are sequential statements here with no early success path between them --
// there is no arrangement of this function in which passing one of them returns an Identity.
type Authenticator struct {
	Reviewer TokenReviewer
	Expected ExpectedCaller
	// TrustDomain is the SPIFFE trust domain the peer certificate must belong to. Empty disables
	// the SPIFFE binding -- permitted only for a broker fronted by a mesh sidecar that has already
	// verified it, and logged loudly at startup, because it removes the binding between the
	// certificate and the token.
	TrustDomain string
	Security    SecuritySink
}

func unauthenticated(reason, detail string, security bool) *Refusal {
	return &Refusal{
		Status:        http.StatusUnauthorized,
		Reason:        reason,
		Detail:        detail,
		SecurityEvent: security,
	}
}

// Refusal reasons specific to the auth layer.
const (
	ReasonMTLSRequired    = "mtls-required"
	ReasonTokenRequired   = "token-required"
	ReasonTokenInvalid    = "token-invalid"
	ReasonAudienceInvalid = "token-audience-invalid"
	ReasonPeerMismatch    = "peer-identity-mismatch"
)

// Authenticate runs both layers and returns the caller's Identity.
//
// Order is deliberate. The transport check runs first because it is free and because a request
// without a client certificate should never reach the API server at all -- forwarding its token
// to a TokenReview would let an unauthenticated peer use the broker as an oracle for whether a
// stolen token is still valid.
func (a *Authenticator) Authenticate(ctx context.Context, r *http.Request) (*Identity, error) {
	peer, err := a.checkTransport(r)
	if err != nil {
		a.emit(ctx, r, err, "")
		return nil, err
	}

	token, err := bearerToken(r)
	if err != nil {
		a.emit(ctx, r, err, peer)
		return nil, err
	}

	status, rErr := a.Reviewer.Review(ctx, token, []string{TokenAudience})
	if rErr != nil {
		// The API server was unreachable or refused the review. This is a 503, not a 401: telling
		// the caller "unauthorized" when we could not determine authorization is how a control
		// plane outage becomes a debugging session about credentials.
		ref := &Refusal{
			Status: http.StatusServiceUnavailable,
			Reason: ReasonTokenInvalid,
			Detail: "TokenReview could not be completed: " + rErr.Error(),
		}
		a.emit(ctx, r, ref, peer)
		return nil, ref
	}

	if !status.Authenticated {
		ref := unauthenticated(ReasonTokenInvalid, "the presented token is not authenticated"+errorSuffix(status.Error), true)
		a.emit(ctx, r, ref, peer)
		return nil, ref
	}

	// V-BRK-017. `status.Authenticated` is true for a DEFAULT-audience ServiceAccount token: the
	// API server happily authenticates it, it just does not carry our audience. A broker that
	// stopped reading at the line above would accept every SA token in the cluster and pass every
	// other check in this file. This is the only place that mistake is visible, which is exactly
	// why the check exists as its own conformance ID.
	if !containsAudience(status.Audiences, TokenAudience) {
		ref := unauthenticated(ReasonAudienceInvalid, fmt.Sprintf(
			"the token authenticates but its audiences are %v; a projected token with audience %q is required",
			status.Audiences, TokenAudience), true)
		a.emit(ctx, r, ref, peer)
		return nil, ref
	}

	ns, sa, ok := parseServiceAccountUsername(status.User.Username)
	if !ok {
		ref := unauthenticated(ReasonTokenInvalid, fmt.Sprintf(
			"caller %q is not a ServiceAccount; the broker accepts exactly one reader ServiceAccount", status.User.Username), true)
		a.emit(ctx, r, ref, peer)
		return nil, ref
	}

	// V-BRK-010. Exactly one accepted reader identity. Constant-time comparison is not about
	// timing here -- the names are not secrets -- it is about not letting a future refactor turn
	// this into a prefix or suffix match, which is how `agent-a-reader` starts matching
	// `agent-a-reader-2`.
	if !equalConstantTime(ns, a.Expected.Namespace) || !equalConstantTime(sa, a.Expected.ServiceAccount) {
		ref := &Refusal{
			Status: http.StatusForbidden,
			Reason: ReasonForbiddenCaller,
			Detail: fmt.Sprintf(
				"caller %s is authenticated but is not this broker's reader; only system:serviceaccount:%s:%s may submit actions here",
				status.User.Username, a.Expected.Namespace, a.Expected.ServiceAccount),
			SecurityEvent: true,
		}
		a.emit(ctx, r, ref, peer)
		return nil, ref
	}

	// Bind the two layers together. Without this the checks are independent: a valid certificate
	// from workload X plus a token stolen from workload Y would satisfy both and be recorded as Y.
	if a.TrustDomain != "" {
		want := spiffeID(a.TrustDomain, ns, sa)
		if !equalConstantTime(peer, want) {
			ref := &Refusal{
				Status: http.StatusForbidden,
				Reason: ReasonPeerMismatch,
				Detail: fmt.Sprintf(
					"the client certificate identifies %q but the token identifies %q; the two layers must agree", peer, want),
				SecurityEvent: true,
			}
			a.emit(ctx, r, ref, peer)
			return nil, ref
		}
	}

	return &Identity{
		Username:       status.User.Username,
		Namespace:      ns,
		ServiceAccount: sa,
		UID:            status.User.UID,
		AgentName:      a.Expected.AgentName,
		Tier:           a.Expected.Tier,
		Scope:          a.Expected.Scope,
		PeerSPIFFE:     peer,
	}, nil
}

// checkTransport enforces mutual TLS (V-BRK-007) and extracts the peer's SPIFFE identity.
func (a *Authenticator) checkTransport(r *http.Request) (string, error) {
	if r.TLS == nil {
		// Cleartext. Only reachable if the server was started without TLS, which main refuses to
		// do -- but the check stays, because "the listener is configured correctly" is an
		// assumption and this is a request handler.
		return "", unauthenticated(ReasonMTLSRequired, "the broker accepts only TLS connections", true)
	}
	if !r.TLS.HandshakeComplete {
		return "", unauthenticated(ReasonMTLSRequired, "the TLS handshake did not complete", true)
	}
	if len(r.TLS.PeerCertificates) == 0 {
		return "", unauthenticated(ReasonMTLSRequired,
			"no client certificate was presented; the broker requires mutual TLS", true)
	}
	if a.TrustDomain == "" {
		return "", nil
	}
	id, err := spiffeFromCert(r.TLS.PeerCertificates[0].URIs, a.TrustDomain)
	if err != nil {
		return "", unauthenticated(ReasonMTLSRequired, err.Error(), true)
	}
	return id, nil
}

func spiffeFromCert(uris []*url.URL, trustDomain string) (string, error) {
	want := "spiffe://" + trustDomain + "/"
	for _, u := range uris {
		if u == nil || u.Scheme != "spiffe" {
			continue
		}
		s := u.String()
		if strings.HasPrefix(s, want) {
			return s, nil
		}
		// A SPIFFE ID from another trust domain is a stronger signal than none at all: it means a
		// certificate from a different mesh was accepted by the TLS layer.
		return "", fmt.Errorf("client certificate carries SPIFFE id %q, which is outside trust domain %q", s, trustDomain)
	}
	return "", fmt.Errorf("client certificate carries no SPIFFE URI SAN in trust domain %q", trustDomain)
}

// SPIFFEID renders a workload's mesh identity. Exported and used by BOTH sides on purpose.
//
// The broker refuses any request whose client-certificate SPIFFE URI does not equal the ID derived
// from the TokenReview (`ReasonPeerMismatch`, above). The controller has to put that exact string
// into the `Certificate` it asks cert-manager to issue. Two independent `fmt.Sprintf`s of the same
// shape would agree today and diverge the first time either side gained a path segment — and the
// failure would not be a compile error or a red unit test, it would be every envelope in the fleet
// refused at the transport layer with a message about trust domains, at L2, after a rollout.
//
// So there is one definition site and both sides call it. `dev/tests/` has three checks whose whole
// job is enforcing that property for other strings (the API group, the scope labels, the broker
// name suffixes); this one is enforced by the type system instead, because both callers are Go.
func SPIFFEID(trustDomain, namespace, serviceAccount string) string {
	return fmt.Sprintf("spiffe://%s/ns/%s/sa/%s", trustDomain, namespace, serviceAccount)
}

func spiffeID(trustDomain, namespace, serviceAccount string) string {
	return SPIFFEID(trustDomain, namespace, serviceAccount)
}

func bearerToken(r *http.Request) (string, error) {
	h := r.Header.Get("Authorization")
	if h == "" {
		return "", unauthenticated(ReasonTokenRequired,
			"no Authorization header; a projected ServiceAccount token with audience "+TokenAudience+" is required", false)
	}
	const prefix = "Bearer "
	if len(h) <= len(prefix) || !strings.EqualFold(h[:len(prefix)], prefix) {
		return "", unauthenticated(ReasonTokenRequired, "the Authorization header is not a Bearer token", false)
	}
	token := strings.TrimSpace(h[len(prefix):])
	if token == "" {
		return "", unauthenticated(ReasonTokenRequired, "the Bearer token is empty", false)
	}
	return token, nil
}

func parseServiceAccountUsername(username string) (namespace, name string, ok bool) {
	const prefix = "system:serviceaccount:"
	if !strings.HasPrefix(username, prefix) {
		return "", "", false
	}
	parts := strings.Split(strings.TrimPrefix(username, prefix), ":")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

func containsAudience(got []string, want string) bool {
	for _, a := range got {
		if a == want {
			return true
		}
	}
	return false
}

func equalConstantTime(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

func errorSuffix(s string) string {
	if s == "" {
		return ""
	}
	return ": " + s
}

// emit forwards a refusal to the security sink when the refusal says it is security-relevant.
// Centralised here so every return path above is one line and none of them can forget it.
func (a *Authenticator) emit(ctx context.Context, r *http.Request, err error, peer string) {
	ref, ok := err.(*Refusal)
	if !ok || !ref.SecurityEvent || a.Security == nil {
		return
	}
	caller := peer
	if caller == "" {
		caller = "unauthenticated"
	}
	a.Security.Security(ctx, SecurityRecord{
		Reason:     ref.Reason,
		Detail:     ref.Detail,
		Caller:     caller,
		RemoteAddr: r.RemoteAddr,
		Path:       r.URL.Path,
	})
}
