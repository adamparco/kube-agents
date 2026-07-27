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
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	authnv1 "k8s.io/api/authentication/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The 08 §2.3 two-layer check, one conformance ID per test.
//
// Every case here is built from a FAKE TokenReviewer, because the responses that matter cannot be
// obtained from a real cluster on demand: V-BRK-017 needs `authenticated: true` with the wrong
// audience, and an API server will not produce that for a token we control. Faking the reviewer
// keeps the assertion on the broker's reading of the response, which is the thing under test.

const (
	testNamespace = "kube-agents-platform"
	testReaderSA  = "platform-agent-reader"
	testUsername  = "system:serviceaccount:" + testNamespace + ":" + testReaderSA
)

// fakeReviewer returns a canned status, and records the audiences it was asked for.
type fakeReviewer struct {
	status   *authnv1.TokenReviewStatus
	err      error
	audience []string
	calls    int
}

func (f *fakeReviewer) Review(_ context.Context, _ string, audiences []string) (*authnv1.TokenReviewStatus, error) {
	f.calls++
	f.audience = audiences
	if f.err != nil {
		return nil, f.err
	}
	return f.status, nil
}

// authenticatedAs is the review response for a token that is valid and carries our audience.
func authenticatedAs(username string) *authnv1.TokenReviewStatus {
	return &authnv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     []string{TokenAudience},
		User:          authnv1.UserInfo{Username: username, UID: "uid-1"},
	}
}

func testAuthenticator(rev TokenReviewer, sink SecuritySink) *Authenticator {
	return &Authenticator{
		Reviewer: rev,
		Expected: ExpectedCaller{
			Namespace:      testNamespace,
			ServiceAccount: testReaderSA,
			AgentName:      "platform-agent",
			Tier:           agentv1alpha1.TierPlatform,
			Scope:          "adamparco-kage",
		},
		TrustDomain: DefaultTrustDomain,
		Security:    sink,
	}
}

// request builds a request whose TLS state carries the given SPIFFE id, or no client certificate
// when spiffe is empty. tlsOn=false produces a cleartext request.
func request(t *testing.T, tlsOn bool, spiffe, authz string) *http.Request {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, ActionsPath, http.NoBody)
	r.RemoteAddr = "10.4.1.7:44321"
	if authz != "" {
		r.Header.Set("Authorization", authz)
	}
	if !tlsOn {
		return r
	}
	state := &tls.ConnectionState{HandshakeComplete: true}
	if spiffe != "" {
		u, err := url.Parse(spiffe)
		if err != nil {
			t.Fatalf("parse spiffe %q: %v", spiffe, err)
		}
		state.PeerCertificates = []*x509.Certificate{{URIs: []*url.URL{u}}}
	}
	r.TLS = state
	return r
}

func readerSPIFFE() string {
	return spiffeID(DefaultTrustDomain, testNamespace, testReaderSA)
}

// refusalOf asserts err is a *Refusal and returns it.
func refusalOf(t *testing.T, err error) *Refusal {
	t.Helper()
	if err == nil {
		t.Fatal("expected a refusal, got nil")
	}
	var ref *Refusal
	if !errors.As(err, &ref) {
		t.Fatalf("expected a *Refusal, got %T: %v", err, err)
	}
	return ref
}

// V-BRK-007: mutual TLS is required. A request with no client certificate -- or no TLS at all --
// never reaches the TokenReview, which is asserted here as well as the status: forwarding the
// token would make the broker an oracle for whether a stolen token is still live.
func TestAuthRequiresMutualTLS(t *testing.T) {
	cases := []struct {
		name   string
		tlsOn  bool
		spiffe string
	}{
		{"cleartext", false, ""},
		{"TLS with no client certificate", true, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rev := &fakeReviewer{status: authenticatedAs(testUsername)}
			sink := &MemorySecuritySink{}
			a := testAuthenticator(rev, sink)

			_, err := a.Authenticate(context.Background(), request(t, c.tlsOn, c.spiffe, "Bearer tok"))
			ref := refusalOf(t, err)
			if ref.Reason != ReasonMTLSRequired {
				t.Fatalf("reason = %q, want %q", ref.Reason, ReasonMTLSRequired)
			}
			if ref.Status != http.StatusUnauthorized {
				t.Fatalf("status = %d, want 401", ref.Status)
			}
			if rev.calls != 0 {
				t.Fatalf("TokenReview was called %d times for a request with no client certificate; the token must not leave the process", rev.calls)
			}
			if len(sink.Records()) != 1 {
				t.Fatalf("security records = %d, want 1", len(sink.Records()))
			}
		})
	}
}

// V-BRK-008: the token must carry audience `kubeagents-broker`, and the broker must ASK for it.
//
// Asserting the request audiences and not only the response is the point. An API server that does
// not support audience validation echoes back whatever it was given; a broker that sent an empty
// audience list would be validating against the API server's own audience and would accept every
// ServiceAccount token in the cluster while this test still passed on the response alone.
func TestAuthRequestsTheBrokerAudience(t *testing.T) {
	rev := &fakeReviewer{status: authenticatedAs(testUsername)}
	a := testAuthenticator(rev, &MemorySecuritySink{})

	id, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "Bearer tok"))
	if err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	if len(rev.audience) != 1 || rev.audience[0] != TokenAudience {
		t.Fatalf("TokenReview requested audiences %v, want exactly [%s]", rev.audience, TokenAudience)
	}
	if id.Tier != agentv1alpha1.TierPlatform || id.Scope != "adamparco-kage" {
		t.Fatalf("identity carries tier=%q scope=%q; both must come from the broker's own config", id.Tier, id.Scope)
	}
	if id.Username != testUsername {
		t.Fatalf("username = %q, want %q", id.Username, testUsername)
	}
}

// V-BRK-017: a DEFAULT-audience ServiceAccount token is refused.
//
// This is the check with the narrowest failure mode in the whole auth layer. TokenReview returns
// `authenticated: true` for such a token -- it IS a valid token, just not for us -- so a broker
// that stops reading at that field passes V-BRK-007, V-BRK-008, V-BRK-009 and V-BRK-010 and
// accepts any pod in the cluster. Only this test distinguishes them.
func TestAuthRefusesDefaultAudienceToken(t *testing.T) {
	rev := &fakeReviewer{status: &authnv1.TokenReviewStatus{
		Authenticated: true,
		// What kube-apiserver returns for a default-mounted token.
		Audiences: []string{"https://kubernetes.default.svc"},
		User:      authnv1.UserInfo{Username: testUsername, UID: "uid-1"},
	}}
	sink := &MemorySecuritySink{}
	a := testAuthenticator(rev, sink)

	_, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "Bearer tok"))
	ref := refusalOf(t, err)
	if ref.Reason != ReasonAudienceInvalid {
		t.Fatalf("reason = %q, want %q -- the broker is accepting a token authenticated for another audience", ref.Reason, ReasonAudienceInvalid)
	}
	if !ref.SecurityEvent || len(sink.Records()) != 1 {
		t.Fatalf("a wrong-audience token must raise a security event; SecurityEvent=%v records=%d", ref.SecurityEvent, len(sink.Records()))
	}

	// And the same caller with the right audience gets through, so the test above is not passing
	// because something unrelated is broken.
	rev.status = authenticatedAs(testUsername)
	if _, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "Bearer tok")); err != nil {
		t.Fatalf("the same caller with the correct audience was refused: %v", err)
	}
}

// V-BRK-009: neither layer alone suffices.
//
// Stated as the full truth table rather than two negative cases, because "both are required" is a
// claim about the CONJUNCTION and only the table shows it: three of the four rows must fail, and
// the one that succeeds must be the one with both.
func TestAuthNeitherLayerAloneSuffices(t *testing.T) {
	cases := []struct {
		name       string
		mTLS       bool
		token      bool
		wantReason string
	}{
		{"neither", false, false, ReasonMTLSRequired},
		{"mTLS only", true, false, ReasonTokenRequired},
		{"token only", false, true, ReasonMTLSRequired},
		{"both", true, true, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			a := testAuthenticator(&fakeReviewer{status: authenticatedAs(testUsername)}, &MemorySecuritySink{})
			spiffe := ""
			if c.mTLS {
				spiffe = readerSPIFFE()
			}
			authz := ""
			if c.token {
				authz = "Bearer tok"
			}
			_, err := a.Authenticate(context.Background(), request(t, c.mTLS, spiffe, authz))
			if c.wantReason == "" {
				if err != nil {
					t.Fatalf("both layers present but Authenticate failed: %v", err)
				}
				return
			}
			if got := refusalOf(t, err).Reason; got != c.wantReason {
				t.Fatalf("reason = %q, want %q", got, c.wantReason)
			}
		})
	}
}

// V-BRK-010: a foreign reader ServiceAccount is refused, with a security event.
//
// The near-miss cases are the interesting ones. `platform-agent-reader-2` and a same-named SA in
// another namespace are both what a prefix or suffix match would let through, and both are exactly
// what an attacker who can create a ServiceAccount would name theirs.
func TestAuthRefusesForeignReader(t *testing.T) {
	for _, username := range []string{
		"system:serviceaccount:kube-agents-platform:some-other-sa",
		"system:serviceaccount:other-namespace:" + testReaderSA,
		"system:serviceaccount:kube-agents-platform:platform-agent-reader-2",
		"system:serviceaccount:kube-agents-platform:latform-agent-reader",
		"system:node:gke-node-1",
		"kubernetes-admin",
	} {
		t.Run(username, func(t *testing.T) {
			sink := &MemorySecuritySink{}
			a := testAuthenticator(&fakeReviewer{status: authenticatedAs(username)}, sink)

			_, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "Bearer tok"))
			ref := refusalOf(t, err)
			if ref.Reason != ReasonForbiddenCaller && ref.Reason != ReasonTokenInvalid {
				t.Fatalf("reason = %q, want forbidden-caller or token-invalid", ref.Reason)
			}
			if !ref.SecurityEvent {
				t.Fatal("a foreign authenticated caller must raise a security event")
			}
			if len(sink.Records()) != 1 {
				t.Fatalf("security records = %d, want 1", len(sink.Records()))
			}
			if sink.Records()[0].Reason != ref.Reason {
				t.Fatalf("security record reason %q does not match the refusal %q", sink.Records()[0].Reason, ref.Reason)
			}
		})
	}
}

// The binding between the two layers. A valid certificate from workload X plus a token stolen from
// workload Y satisfies both checks independently; without this the action would be attributed to Y
// and the certificate would be decoration.
func TestAuthBindsCertificateToToken(t *testing.T) {
	sink := &MemorySecuritySink{}
	a := testAuthenticator(&fakeReviewer{status: authenticatedAs(testUsername)}, sink)

	other := spiffeID(DefaultTrustDomain, testNamespace, "some-other-workload")
	_, err := a.Authenticate(context.Background(), request(t, true, other, "Bearer tok"))
	ref := refusalOf(t, err)
	if ref.Reason != ReasonPeerMismatch {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonPeerMismatch)
	}
	if ref.Status != http.StatusForbidden || !ref.SecurityEvent {
		t.Fatalf("status=%d securityEvent=%v, want 403 and a security event", ref.Status, ref.SecurityEvent)
	}
}

// A certificate from another mesh. Refused at the transport check, before the token is read at
// all, and refused for a reason that says it was the certificate.
func TestAuthRefusesForeignTrustDomain(t *testing.T) {
	rev := &fakeReviewer{status: authenticatedAs(testUsername)}
	a := testAuthenticator(rev, &MemorySecuritySink{})

	_, err := a.Authenticate(context.Background(),
		request(t, true, "spiffe://evil.example/ns/x/sa/y", "Bearer tok"))
	if got := refusalOf(t, err).Reason; got != ReasonMTLSRequired {
		t.Fatalf("reason = %q, want %q", got, ReasonMTLSRequired)
	}
	if rev.calls != 0 {
		t.Fatalf("TokenReview was called for a certificate outside the trust domain")
	}
}

// A TokenReview that could not be completed is a 503, not a 401.
//
// The distinction is operational, not pedantic: 401 sends whoever is on call to look at
// credentials, and the actual fault is the control plane. It also must NOT be a security event --
// an API server outage would otherwise fill the security stream with alarms about itself.
func TestAuthTokenReviewFailureIs503(t *testing.T) {
	sink := &MemorySecuritySink{}
	a := testAuthenticator(&fakeReviewer{err: errors.New("connection refused")}, sink)

	_, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "Bearer tok"))
	ref := refusalOf(t, err)
	if ref.Status != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503; an unreachable API server is not an authorization failure", ref.Status)
	}
	if ref.SecurityEvent || len(sink.Records()) != 0 {
		t.Fatalf("a control-plane outage must not raise a security event (records=%d)", len(sink.Records()))
	}
}

// Malformed Authorization headers. Each is a 401 with token-required, and none of them reaches the
// API server -- an empty or malformed header is not worth a round trip and a broker that made one
// per request would amplify a flood into API-server load.
func TestAuthRejectsMalformedAuthorizationHeaders(t *testing.T) {
	for _, h := range []string{"tok", "Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Bearer   "} {
		t.Run(h, func(t *testing.T) {
			rev := &fakeReviewer{status: authenticatedAs(testUsername)}
			a := testAuthenticator(rev, &MemorySecuritySink{})
			_, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), h))
			if got := refusalOf(t, err).Reason; got != ReasonTokenRequired {
				t.Fatalf("reason = %q, want %q", got, ReasonTokenRequired)
			}
			if rev.calls != 0 {
				t.Fatalf("TokenReview was called for a malformed Authorization header")
			}
		})
	}
	// "bearer" lowercase IS valid: RFC 7235 makes the scheme case-insensitive, and a broker that
	// refused it would be rejecting conformant clients for a reason that reads like a security
	// control and is not one.
	rev := &fakeReviewer{status: authenticatedAs(testUsername)}
	a := testAuthenticator(rev, &MemorySecuritySink{})
	if _, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "bearer tok")); err != nil {
		t.Fatalf("lowercase `bearer` was refused: %v", err)
	}
}

// An unauthenticated review response. Distinct from a wrong-audience one, and it must stay
// distinct: they have different remediations and collapsing them into one reason makes the
// V-BRK-017 case indistinguishable in a log.
func TestAuthRefusesUnauthenticatedToken(t *testing.T) {
	a := testAuthenticator(&fakeReviewer{status: &authnv1.TokenReviewStatus{
		Authenticated: false,
		Error:         "token expired",
	}}, &MemorySecuritySink{})

	_, err := a.Authenticate(context.Background(), request(t, true, readerSPIFFE(), "Bearer tok"))
	ref := refusalOf(t, err)
	if ref.Reason != ReasonTokenInvalid {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonTokenInvalid)
	}
	if !ref.SecurityEvent {
		t.Fatal("an unauthenticated token must raise a security event")
	}
}

// parseServiceAccountUsername on the shapes an API server can actually return.
func TestParseServiceAccountUsername(t *testing.T) {
	cases := []struct {
		in     string
		ns, sa string
		ok     bool
	}{
		{"system:serviceaccount:ns:sa", "ns", "sa", true},
		{"system:serviceaccount:kube-agents-platform:platform-agent-reader", "kube-agents-platform", "platform-agent-reader", true},
		{"system:serviceaccount::sa", "", "", false},
		{"system:serviceaccount:ns:", "", "", false},
		{"system:serviceaccount:ns", "", "", false},
		// Three colons. Not a namespace called "ns:extra" -- refused, because a username the broker
		// cannot parse unambiguously is a username it must not attribute an action to.
		{"system:serviceaccount:ns:sa:extra", "", "", false},
		{"system:node:gke-node-1", "", "", false},
		{"", "", "", false},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			ns, sa, ok := parseServiceAccountUsername(c.in)
			if ok != c.ok || ns != c.ns || sa != c.sa {
				t.Fatalf("got (%q, %q, %v), want (%q, %q, %v)", ns, sa, ok, c.ns, c.sa, c.ok)
			}
		})
	}
}

// AgentIdentity is what the idempotency key and the journal index are built from, so its spelling
// is a contract: change it and every key in flight changes with it.
func TestAgentIdentitySpelling(t *testing.T) {
	cases := []struct {
		tier  agentv1alpha1.AgentTier
		scope string
		want  string
	}{
		{agentv1alpha1.TierPlatform, "adamparco-kage", "platform/adamparco-kage"},
		{agentv1alpha1.TierClusterAdmin, "adamparco-kage/prod-east", "cluster-admin/adamparco-kage/prod-east"},
		{agentv1alpha1.TierDeveloperTeam, "adamparco-kage/prod-east/checkout", "developer-team/adamparco-kage/prod-east/checkout"},
		{agentv1alpha1.TierPlatform, "", "platform"},
	}
	for _, c := range cases {
		id := &Identity{Tier: c.tier, Scope: c.scope}
		if got := id.AgentIdentity(); got != c.want {
			t.Fatalf("AgentIdentity() = %q, want %q", got, c.want)
		}
	}
}
