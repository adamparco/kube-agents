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

package controller

import (
	"net/url"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

// The mesh certificates (08 §2.3, P9-T7d) at L1.
//
// Every property here is one that is otherwise only observable at L2, after cert-manager has
// issued and both pods have started — and most of them fail there as a TLS handshake error, which
// is the least informative way any of these could be reported. The whole point of this file is to
// move them to a place where they fail as a named assertion instead.

func meshAgent() *agentv1alpha1.Agent {
	return pausableAgent(nil)
}

func specOf(t *testing.T, u *unstructured.Unstructured) map[string]any {
	t.Helper()
	spec, ok, err := unstructured.NestedMap(u.Object, "spec")
	if err != nil || !ok {
		t.Fatalf("Certificate %q has no spec: ok=%v err=%v", u.GetName(), ok, err)
	}
	return spec
}

func stringSlice(t *testing.T, spec map[string]any, field string) []string {
	t.Helper()
	raw, ok := spec[field]
	if !ok {
		return nil
	}
	items, ok := raw.([]any)
	if !ok {
		t.Fatalf("%s is %T, want []any", field, raw)
	}
	out := make([]string, 0, len(items))
	for _, it := range items {
		s, ok := it.(string)
		if !ok {
			t.Fatalf("%s contains %T, want string", field, it)
		}
		out = append(out, s)
	}
	return out
}

// TestAgentMeshCertificateCarriesTheSPIFFEIDTheBrokerWillDemand is the load-bearing one.
//
// internal/broker/auth.go refuses any request whose client-certificate SPIFFE URI is not exactly
// the ID derived from the caller's TokenReview, and reports it as `PeerMismatch` — a message about
// trust domains. If this renderer emitted a URI of any other shape, the result would be a fleet
// where every envelope is refused, discoverable only after a rollout, presenting as a mesh problem
// rather than as a rendering problem.
//
// The expected value is computed by calling broker.SPIFFEID, the same function the broker calls,
// so this test cannot pass by agreeing with a copy of the format that has drifted. It also asserts
// the literal shape once, which is what catches a change to SPIFFEID ITSELF — the two callers would
// still agree with each other while both being wrong about what a SPIFFE ID is.
func TestAgentMeshCertificateCarriesTheSPIFFEIDTheBrokerWillDemand(t *testing.T) {
	agent := meshAgent()
	spec := specOf(t, buildAgentMeshCertificate(agent))

	uris := stringSlice(t, spec, "uris")
	if len(uris) != 1 {
		t.Fatalf("agent mesh Certificate has %d URIs, want exactly 1: the broker reads "+
			"PeerCertificates[0].URIs and takes the first SPIFFE ID in its trust domain, so a second "+
			"one is either ignored or ambiguous depending on ordering", len(uris))
	}

	want := broker.SPIFFEID(broker.DefaultTrustDomain, agent.Namespace, readerServiceAccountName(agent))
	if uris[0] != want {
		t.Errorf("agent mesh SPIFFE URI is %q, want %q.\nThe broker derives this value from the "+
			"TokenReview and refuses the request with ReasonPeerMismatch if the certificate disagrees "+
			"(internal/broker/auth.go). Both sides must call broker.SPIFFEID.", uris[0], want)
	}

	// And pin the shape itself, so a change to SPIFFEID is visible here rather than only in the
	// broker's own tests. `spiffe://<trust-domain>/ns/<ns>/sa/<sa>` is what a Kubernetes SPIFFE
	// identity looks like; anything else stops being interoperable with the wider ecosystem even
	// if both halves of THIS system continue to agree.
	if got := uris[0]; got != "spiffe://cluster.local/ns/team-x/sa/"+readerServiceAccountName(agent) {
		t.Errorf("SPIFFE ID shape changed: %q. Expected spiffe://<trust-domain>/ns/<ns>/sa/<sa>", got)
	}

	// A URI SAN must parse as one, or cert-manager rejects the Certificate at admission and the
	// pair never gets a keypair at all.
	u, err := url.Parse(uris[0])
	if err != nil || u.Scheme != "spiffe" || u.Host == "" {
		t.Errorf("SPIFFE URI %q does not parse as a spiffe:// URL (err=%v)", uris[0], err)
	}
}

// TestBothEndsChainToOneIssuer is the property that made T7d a separate unit from T7b.
//
// Self-signing each end gives two certificates that verify against themselves and neither against
// the other. That is not a subtle failure — it is a handshake that never completes — but it is a
// failure with a confusing error, and it is the specific mistake that was one line away while T7b
// was being written.
func TestBothEndsChainToOneIssuer(t *testing.T) {
	agent := meshAgent()

	for name, cert := range map[string]*unstructured.Unstructured{
		"broker":     buildBrokerCertificate(agent),
		"agent mesh": buildAgentMeshCertificate(agent),
	} {
		spec := specOf(t, cert)
		ref, ok := spec["issuerRef"].(map[string]any)
		if !ok {
			t.Fatalf("%s Certificate has no issuerRef", name)
		}
		if ref["kind"] != "ClusterIssuer" {
			t.Errorf("%s issuerRef.kind = %v, want ClusterIssuer. A namespaced Issuer would need the "+
				"mesh CA private key copied into every agent namespace", name, ref["kind"])
		}
		if ref["name"] != meshCAIssuerName {
			t.Errorf("%s issuerRef.name = %v, want %q", name, ref["name"], meshCAIssuerName)
		}
		if ref["group"] != certManagerGroup {
			t.Errorf("%s issuerRef.group = %v, want %q", name, ref["group"], certManagerGroup)
		}
	}
}

// TestMeshCertificateKeysRotateOnRenewal.
//
// cert-manager REUSES the existing private key on renewal unless rotationPolicy is Always. The
// default is therefore a renewal that rotates the certificate and not the secret it protects, which
// looks like key rotation on a dashboard and is not: a leaked key stays valid for as long as the
// workload exists. Silent, and the wrong direction, so it is pinned.
func TestMeshCertificateKeysRotateOnRenewal(t *testing.T) {
	agent := meshAgent()
	for name, cert := range map[string]*unstructured.Unstructured{
		"broker":     buildBrokerCertificate(agent),
		"agent mesh": buildAgentMeshCertificate(agent),
	} {
		pk, ok := specOf(t, cert)["privateKey"].(map[string]any)
		if !ok {
			t.Fatalf("%s Certificate has no privateKey block", name)
		}
		if pk["rotationPolicy"] != "Always" {
			t.Errorf("%s privateKey.rotationPolicy = %v, want Always — otherwise renewal reuses the "+
				"key and a disclosed key survives every rotation", name, pk["rotationPolicy"])
		}
		if pk["algorithm"] != "ECDSA" {
			t.Errorf("%s privateKey.algorithm = %v, want ECDSA", name, pk["algorithm"])
		}
	}
}

// TestBrokerCertificateNamesWhatTheAgentPins.
//
// The agent's TLS client sets ServerName from `brokerSAN` (T7b injects it as KUBEAGENTS_BROKER_SAN
// and the wait-for-broker init container pins it). A broker certificate that does not carry that
// exact DNS name fails verification on every connection — including the init container's health
// poll, which would then report `unavailable` and drop the agent into observe-and-report. A
// misissued certificate would therefore present as a broker outage.
func TestBrokerCertificateNamesWhatTheAgentPins(t *testing.T) {
	agent := meshAgent()
	dns := stringSlice(t, specOf(t, buildBrokerCertificate(agent)), "dnsNames")

	want := brokerSAN(agent)
	found := false
	for _, d := range dns {
		if d == want {
			found = true
		}
	}
	if !found {
		t.Errorf("broker Certificate dnsNames = %v, missing %q — the name the agent pins as "+
			"ServerName. Verification fails on every connection and reports as a broker outage.", dns, want)
	}
	if !strings.HasSuffix(want, ".svc.cluster.local") {
		t.Errorf("brokerSAN %q is not a cluster FQDN; a short name resolves through the pod search path", want)
	}
}

// TestTheTwoEndsHaveDifferentUsages.
//
// The broker listens and the agent dials, so exactly one of them needs `server auth` and exactly
// one needs `client auth`. Granting both to both is the natural shortcut and it matters here more
// than usual: the client certificate is half of the broker's identity check, so a broker keypair
// that is also usable as a client credential is a spare mesh identity sitting in the namespace,
// mounted into the one pod that already holds write authority.
func TestTheTwoEndsHaveDifferentUsages(t *testing.T) {
	agent := meshAgent()

	brokerUsages := stringSlice(t, specOf(t, buildBrokerCertificate(agent)), "usages")
	agentUsages := stringSlice(t, specOf(t, buildAgentMeshCertificate(agent)), "usages")

	has := func(list []string, want string) bool {
		for _, u := range list {
			if u == want {
				return true
			}
		}
		return false
	}

	if !has(brokerUsages, "server auth") {
		t.Errorf("broker usages %v lack 'server auth'", brokerUsages)
	}
	if has(brokerUsages, "client auth") {
		t.Errorf("broker usages %v include 'client auth': the broker's outbound calls authenticate "+
			"by ServiceAccount token, so this keypair would be a spare mesh identity mounted into the "+
			"pod that holds the write credential", brokerUsages)
	}
	if !has(agentUsages, "client auth") {
		t.Errorf("agent usages %v lack 'client auth'", agentUsages)
	}
	if has(agentUsages, "server auth") {
		t.Errorf("agent usages %v include 'server auth': nothing dials the agent on this keypair", agentUsages)
	}
}

// TestMeshCertificatesAreNamedAndLabelledPerAgent.
//
// V-BRK-012's geometry applied to the trust material: two agents in one namespace (08 §2.6 puts a
// platform and a cluster-admin broker in kubeagents-system together) must not collide on a
// Certificate name or on a Secret name, or one pair silently presents the other's identity. LSN-015
// is why this renders two CRs rather than one — every derivation here is per-CR, and a single
// fixture cannot fail a collision test.
//
// # The two agents differ in SCOPE, and that is not fixture decoration
//
// Writing this test with two same-scope agents made it fail, and working out why is the finding.
// The four values involved live in TWO uniqueness domains held up by two different mechanisms:
//
//   - Certificate and Secret names derive from `agent.Name`, unique per namespace because the API
//     server says so.
//   - The ACTOR SPIFFE ID derives from `(tier, scope)` and not from the name at all (06 §5.1: the
//     ability to name the actor identity is the ability to choose an authority level, so the name
//     cannot be an input). Its uniqueness rests on admission enforcing (tier, scope) uniqueness
//     fleet-wide — `agent_webhook.validateScopeAndParent`.
//   - The READER SPIFFE ID derives from `agent.Name` again (or `spec.security.serviceAccountName`).
//
// So two same-tier same-scope agents would get distinct certificates carrying the SAME actor
// identity. That configuration is unrepresentable — admission refuses it — but the dependency is
// worth stating out loud, because it means the mesh's identity uniqueness is a property of the
// WEBHOOK, not of this renderer. If that admission rule were ever relaxed, this file would start
// issuing duplicate identities and nothing here would notice.
func TestMeshCertificatesAreNamedAndLabelledPerAgent(t *testing.T) {
	a := pausableAgent(nil)
	a.Name = "alpha"
	a.Namespace = "kubeagents-system"
	a.Spec.Scope = &agentv1alpha1.ScopeSpec{ProjectID: "p", ClusterName: "c", Namespace: "team-x"}

	b := pausableAgent(nil)
	b.Name = "beta"
	b.Namespace = "kubeagents-system"
	b.Spec.Scope = &agentv1alpha1.ScopeSpec{ProjectID: "p", ClusterName: "c", Namespace: "team-y"}

	names := map[string]string{}
	secrets := map[string]string{}
	uris := map[string]string{}

	for _, agent := range []*agentv1alpha1.Agent{a, b} {
		for label, cert := range map[string]*unstructured.Unstructured{
			"broker": buildBrokerCertificate(agent),
			"mesh":   buildAgentMeshCertificate(agent),
		} {
			key := agent.Name + "/" + label
			if prior, dup := names[cert.GetName()]; dup {
				t.Errorf("Certificate name %q is produced by both %s and %s", cert.GetName(), prior, key)
			}
			names[cert.GetName()] = key

			spec := specOf(t, cert)
			secretName, _ := spec["secretName"].(string)
			if prior, dup := secrets[secretName]; dup {
				t.Errorf("secretName %q is produced by both %s and %s — one pair would present the "+
					"other's identity", secretName, prior, key)
			}
			secrets[secretName] = key

			u := stringSlice(t, spec, "uris")
			if len(u) == 1 {
				if prior, dup := uris[u[0]]; dup {
					t.Errorf("SPIFFE ID %q is claimed by both %s and %s — two workloads with one mesh "+
						"identity means the broker's peer/token binding cannot tell them apart", u[0], prior, key)
				}
				uris[u[0]] = key
			}

			if got := cert.GetLabels()[agentlabels.Agent]; got != agent.Name {
				t.Errorf("%s Certificate label %s = %q, want %q", key, agentlabels.Agent, got, agent.Name)
			}
			if got := cert.GetNamespace(); got != agent.Namespace {
				t.Errorf("%s Certificate namespace = %q, want %q", key, got, agent.Namespace)
			}
		}
	}

	// The two roles must be distinguishable by label, because the NetworkPolicies in T7d-2 select
	// on exactly this pair of keys.
	if buildBrokerCertificate(a).GetLabels()[agentlabels.Role] != agentlabels.RoleActor {
		t.Error("broker Certificate is not labelled role=actor")
	}
	if buildAgentMeshCertificate(a).GetLabels()[agentlabels.Role] != agentlabels.RoleReader {
		t.Error("agent mesh Certificate is not labelled role=reader")
	}
}

// TestMeshCertificatesMountWhatTheDeploymentsExpect closes the loop with T7b.
//
// T7b's Deployments mount `<agent>-broker-tls` and `<agent>-mesh-tls` by name. This renderer decides
// what cert-manager will actually call those Secrets. The two derivations live in different files
// and nothing but this assertion connects them; if they diverge, both halves render successfully,
// both pods stay in ContainerCreating forever, and the CR reports BrokerReady: false with no clue
// as to why.
func TestMeshCertificatesMountWhatTheDeploymentsExpect(t *testing.T) {
	agent := meshAgent()

	cases := map[string]struct {
		cert *unstructured.Unstructured
		want string
	}{
		"broker": {buildBrokerCertificate(agent), brokerTLSSecretName(agent)},
		"agent":  {buildAgentMeshCertificate(agent), agentMeshTLSSecretName(agent)},
	}
	for name, tc := range cases {
		got, _ := specOf(t, tc.cert)["secretName"].(string)
		if got != tc.want {
			t.Errorf("%s Certificate secretName = %q, but the Deployment mounts %q", name, got, tc.want)
		}
	}

	// And the Deployments really do mount those names — otherwise this test agrees with a renderer
	// nobody uses. Read them back off the rendered pair rather than off the helper functions.
	pair := newWorkloadPair(buildBrokerDeployment(agent), buildDeployment(agent, "c", "f", "s"))
	mounted := map[string]bool{}
	for _, dep := range pair.Ordered() {
		for _, v := range dep.Spec.Template.Spec.Volumes {
			if v.Secret != nil {
				mounted[v.Secret.SecretName] = true
			}
		}
	}
	for _, want := range []string{brokerTLSSecretName(agent), agentMeshTLSSecretName(agent)} {
		if !mounted[want] {
			t.Errorf("no Deployment in the pair mounts Secret %q, but a Certificate creates it. "+
				"Either the mount was dropped or this unit is issuing a keypair nothing uses.", want)
		}
	}
}
