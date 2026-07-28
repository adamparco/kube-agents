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

// broker_manifests_test.go — V-RUN-003 (the pair renders correctly and in order) and the Go half
// of V-BRK-012 (one broker per Agent CR, no fleet-wide writer) at L0.
//
// V-BRK-012's other half is dev/tests/one-broker-per-agent.py, a source lint. Both are needed and
// neither subsumes the other: the lint catches a broker name that stops depending on the CR, which
// no amount of single-CR rendering would reveal, and this file catches two CRs whose renders
// collide, which the lint cannot see because the collision is in the values, not the source.

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

func brokerTestAgent(name, namespace string, tier agentv1alpha1.AgentTier, scope *agentv1alpha1.ScopeSpec) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec:       agentv1alpha1.AgentSpec{Tier: tier, Scope: scope},
	}
}

// TestBrokerCanonicalNames pins the names of 08 §2.1. They are spelled in NetworkPolicies, in the
// GitOps exemplars, and in the L2 scripts, so a rename that only updates the renderer produces a
// mesh where each of those selects nothing — and selecting nothing is not an error anywhere in
// Kubernetes.
func TestBrokerCanonicalNames(t *testing.T) {
	agent := brokerTestAgent("payments", "team-payments", agentv1alpha1.TierDeveloperTeam, &agentv1alpha1.ScopeSpec{
		ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments",
	})

	if got := brokerName(agent); got != "payments-broker" {
		t.Errorf("broker name: want payments-broker, got %q", got)
	}
	if got := brokerTLSSecretName(agent); got != "payments-broker-tls" {
		t.Errorf("broker TLS secret: want payments-broker-tls, got %q", got)
	}
	if got := agentMeshTLSSecretName(agent); got != "payments-mesh-tls" {
		t.Errorf("agent mesh TLS secret: want payments-mesh-tls, got %q", got)
	}
	want := "https://payments-broker.team-payments.svc.cluster.local:8443"
	if got := brokerEndpoint(agent); got != want {
		t.Errorf("endpoint: want %s, got %s", want, got)
	}
	// The SAN must be the endpoint's host exactly. If they drift, the agent pins a name the
	// broker's certificate does not carry and every connection fails the handshake — or, worse, a
	// SAN loosened to make that "work" stops proving which broker answered.
	if !strings.Contains(brokerEndpoint(agent), brokerSAN(agent)) {
		t.Errorf("SAN %q is not the endpoint %q's host", brokerSAN(agent), brokerEndpoint(agent))
	}
}

// TestActorServiceAccountName covers 06 §5.1's derivation, including the truncation arm.
func TestActorServiceAccountName(t *testing.T) {
	longLeaf := strings.Repeat("n", 300)

	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.Agent
		want  string
	}{
		{
			"platform takes the project",
			brokerTestAgent("p", "kubeagents-system", agentv1alpha1.TierPlatform,
				&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod"}),
			"platform-acme-prod-actor",
		},
		{
			"cluster-admin takes the cluster",
			brokerTestAgent("c", "kubeagents-system", agentv1alpha1.TierClusterAdmin,
				&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1"}),
			"cluster-admin-eu-1-actor",
		},
		{
			"developer-team takes the namespace",
			brokerTestAgent("d", "team-payments", agentv1alpha1.TierDeveloperTeam,
				&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments"}),
			"developer-team-team-payments-actor",
		},
		{
			// An empty tier means platform (agentindex.EffectiveTier), and stamping "" here would
			// produce `-acme-prod-actor` — a name that is both invalid and, being a different
			// string, a DIFFERENT identity from the one the same agent gets after a CRD default
			// backfills its tier.
			"an empty tier resolves to platform",
			brokerTestAgent("p", "kubeagents-system", "",
				&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod"}),
			"platform-acme-prod-actor",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := actorServiceAccountName(tc.agent); got != tc.want {
				t.Errorf("want %q, got %q", tc.want, got)
			}
		})
	}

	t.Run("over 253 characters truncates the leaf and suffixes a digest", func(t *testing.T) {
		agent := brokerTestAgent("d", "ns", agentv1alpha1.TierDeveloperTeam, &agentv1alpha1.ScopeSpec{
			ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: longLeaf,
		})
		got := actorServiceAccountName(agent)
		if len(got) > actorSANameLimit {
			t.Errorf("name is %d characters, over the %d limit: %q", len(got), actorSANameLimit, got)
		}
		if !strings.HasPrefix(got, "developer-team-"+strings.Repeat("n", actorSALeafLimit)+"-") {
			t.Errorf("expected the leaf truncated to %d characters, got %q", actorSALeafLimit, got)
		}
		if !strings.HasSuffix(got, "-actor") {
			t.Errorf("expected the -actor suffix, got %q", got)
		}
	})

	t.Run("two long leaves sharing a prefix get different names", func(t *testing.T) {
		// The whole reason the truncation arm carries a digest. Without it, every namespace whose
		// first 40 characters match would resolve to ONE actor ServiceAccount — one credential
		// shared across tenants, which is the failure 06 §5.1's suffix exists to prevent.
		a := brokerTestAgent("a", "ns", agentv1alpha1.TierDeveloperTeam, &agentv1alpha1.ScopeSpec{
			ProjectID: "p", ClusterName: "c", Namespace: longLeaf + "-alpha",
		})
		b := brokerTestAgent("b", "ns", agentv1alpha1.TierDeveloperTeam, &agentv1alpha1.ScopeSpec{
			ProjectID: "p", ClusterName: "c", Namespace: longLeaf + "-beta",
		})
		if actorServiceAccountName(a) == actorServiceAccountName(b) {
			t.Fatalf("two distinct scopes collided on %q", actorServiceAccountName(a))
		}
	})
}

// TestBrokerDeploymentPosture covers 08 §2.6's harder posture for the credential holder.
func TestBrokerDeploymentPosture(t *testing.T) {
	agent := brokerTestAgent("payments", "team-payments", agentv1alpha1.TierDeveloperTeam,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments"})
	dep := buildBrokerDeployment(agent)

	pod := dep.Spec.Template.Spec
	if pod.ServiceAccountName != actorServiceAccountName(agent) {
		t.Errorf("broker must run as the actor SA, got %q", pod.ServiceAccountName)
	}
	if len(pod.Containers) != 1 {
		t.Fatalf("the broker pod is one container (08 §2.2), got %d", len(pod.Containers))
	}

	c := pod.Containers[0]
	sc := c.SecurityContext
	switch {
	case sc == nil:
		t.Fatal("broker container has no securityContext")
	case sc.ReadOnlyRootFilesystem == nil || !*sc.ReadOnlyRootFilesystem:
		t.Error("08 §2.6 requires a read-only root filesystem on the broker")
	case sc.AllowPrivilegeEscalation == nil || *sc.AllowPrivilegeEscalation:
		t.Error("allowPrivilegeEscalation must be false")
	case sc.RunAsNonRoot == nil || !*sc.RunAsNonRoot:
		t.Error("runAsNonRoot must be true")
	}
	if sc != nil && (sc.Capabilities == nil || len(sc.Capabilities.Drop) != 1 || sc.Capabilities.Drop[0] != "ALL") {
		t.Errorf("all capabilities must be dropped, got %+v", sc.Capabilities)
	}

	// 08 §2.6: "no volume mounts other than its certificate Secret and its projected token". The
	// /tmp emptyDir is the one addition, and it exists only because a read-only root without it is
	// a runtime crash rather than a hardening measure. Anything BEYOND these is a path by which
	// state reaches the credential holder, so the assertion is on the exact set.
	allowed := map[string]bool{brokerTLSVolumeName: true, brokerTmpVolumeName: true}
	for _, m := range c.VolumeMounts {
		if !allowed[m.Name] {
			t.Errorf("unexpected mount %q on the broker: 08 §2.6 allows only its certificate and its token", m.Name)
		}
	}

	// A kubelet httpGet probe cannot complete a RequireAndVerifyClientCert handshake, so an
	// httpGet here would report the broker permanently unhealthy and CrashLoop it. If someone
	// "fixes" that by relaxing the listener, V-BRK-007 goes red — but this catches the other
	// direction, where the probe is changed and the listener is not.
	if c.ReadinessProbe == nil || c.ReadinessProbe.TCPSocket == nil {
		t.Error("readiness must be a tcpSocket probe: the broker requires a client certificate the kubelet does not have")
	}
	if c.ReadinessProbe != nil && c.ReadinessProbe.HTTPGet != nil {
		t.Error("an httpGet readiness probe cannot handshake with the broker and will CrashLoop it")
	}

	if *dep.Spec.Replicas != 1 {
		t.Errorf("one broker per agent, got %d replicas", *dep.Spec.Replicas)
	}
}

// TestBrokerServiceSelectsBothLabels is the conjunction of 08 §2.3. Either label alone is a
// misroute: `role: actor` alone reaches a co-located agent's broker (a scope escape wearing the
// shape of load balancing), and `agent: <name>` alone reaches the agent pod, which has no envelope
// listener at all.
func TestBrokerServiceSelectsBothLabels(t *testing.T) {
	agent := brokerTestAgent("payments", "team-payments", agentv1alpha1.TierDeveloperTeam,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments"})
	svc := buildBrokerService(agent)

	if got := svc.Spec.Selector[agentlabels.Role]; got != agentlabels.RoleActor {
		t.Errorf("selector must pin role=actor, got %q", got)
	}
	if got := svc.Spec.Selector[agentlabels.Agent]; got != "payments" {
		t.Errorf("selector must pin agent=payments, got %q", got)
	}
	if len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != broker.Port {
		t.Errorf("the broker exposes exactly one port, %d: got %+v", broker.Port, svc.Spec.Ports)
	}
	if svc.Spec.Ports[0].Name != brokerPortName {
		t.Errorf("port name must be %q (08 §2.1), got %q", brokerPortName, svc.Spec.Ports[0].Name)
	}
}

// TestAgentPodCarriesAudienceScopedToken is 08 §2.3's "a projected SA token with audience
// kubeagents-broker, not the default API token".
//
// The default token is accepted by the API server. A broker that took it would accept a credential
// minted for something else entirely, and every process in the agent pod can already read that
// file. An audience the API server will not honour makes the token useless anywhere but the
// broker's door — which is what turns "the agent has a token" from a risk into a mechanism.
func TestAgentPodCarriesAudienceScopedToken(t *testing.T) {
	agent := brokerTestAgent("payments", "team-payments", agentv1alpha1.TierDeveloperTeam,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments"})
	dep := buildDeployment(agent, "h1", "h2", "h3")

	var projected *corev1.ServiceAccountTokenProjection
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name != agentBrokerTokenVolumeName || v.Projected == nil {
			continue
		}
		for _, s := range v.Projected.Sources {
			if s.ServiceAccountToken != nil {
				projected = s.ServiceAccountToken
			}
		}
	}
	if projected == nil {
		t.Fatalf("no projected token volume %q on the agent pod", agentBrokerTokenVolumeName)
	}
	if projected.Audience != broker.TokenAudience {
		t.Errorf("audience must be %q, got %q", broker.TokenAudience, projected.Audience)
	}
	if projected.ExpirationSeconds == nil || *projected.ExpirationSeconds > 3600 {
		t.Errorf("the broker token must be short-lived, got %v", projected.ExpirationSeconds)
	}
	if !strings.HasPrefix(agentBrokerTokenMountPath, "/var/run/secrets/kubeagents") {
		t.Errorf("the broker token must not share the default token's directory, got %q", agentBrokerTokenMountPath)
	}
}

// TestBrokerEndpointNotOverridableBySpecEnv covers the render-order property in buildDeployment: a
// CR that sets KUBEAGENTS_BROKER_ENDPOINT or KUBEAGENTS_BROKER_SAN in spec.deployment.env must
// lose. Together those two would route a signed envelope AND the projected token to a listener of
// the CR author's choosing, with the agent accepting whatever certificate it presented.
func TestBrokerEndpointNotOverridableBySpecEnv(t *testing.T) {
	agent := brokerTestAgent("payments", "team-payments", agentv1alpha1.TierDeveloperTeam,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments"})
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{
			{Name: "KUBEAGENTS_BROKER_ENDPOINT", Value: "https://attacker.example.com:8443"},
			{Name: "KUBEAGENTS_BROKER_SAN", Value: "attacker.example.com"},
		},
	}
	dep := buildDeployment(agent, "h1", "h2", "h3")

	seen := map[string]int{}
	for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
		switch e.Name {
		case "KUBEAGENTS_BROKER_ENDPOINT":
			seen[e.Name]++
			if e.Value != brokerEndpoint(agent) {
				t.Errorf("endpoint was overridden by spec.deployment.env: %q", e.Value)
			}
		case "KUBEAGENTS_BROKER_SAN":
			seen[e.Name]++
			if e.Value != brokerSAN(agent) {
				t.Errorf("SAN was overridden by spec.deployment.env: %q", e.Value)
			}
		}
	}
	// One entry each, not two. A duplicate would leave the winner to how the kubelet folds the
	// list, which is a detail no spec here pins.
	for _, name := range []string{"KUBEAGENTS_BROKER_ENDPOINT", "KUBEAGENTS_BROKER_SAN"} {
		if seen[name] != 1 {
			t.Errorf("expected exactly one %s entry, got %d", name, seen[name])
		}
	}
}

// TestWaitForBrokerIsFirstAndObserveAndReport covers 08 §2.4's init container.
func TestWaitForBrokerIsFirstAndObserveAndReport(t *testing.T) {
	agent := brokerTestAgent("payments", "team-payments", agentv1alpha1.TierDeveloperTeam,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1", Namespace: "team-payments"})
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		InitContainers: []corev1.Container{{Name: "cr-supplied", Image: "busybox:1.36"}},
	}
	dep := buildDeployment(agent, "h1", "h2", "h3")

	inits := dep.Spec.Template.Spec.InitContainers
	if len(inits) != 2 || inits[0].Name != "wait-for-broker" {
		t.Fatalf("wait-for-broker must run before any CR-supplied init container, got %+v", initNames(inits))
	}
	// It runs the broker image because the poll is a real mTLS handshake and the agent image has
	// no guaranteed TLS client. Asserted so a later "simplification" to curl-in-the-agent-image
	// shows up here rather than as a pod that cannot tell "broker down" from "no certificate".
	if inits[0].Image != brokerImage() {
		t.Errorf("wait-for-broker must run the broker image, got %q", inits[0].Image)
	}
	joined := strings.Join(inits[0].Args, " ")
	for _, want := range []string{"--wait-for-broker", "--broker-endpoint=" + brokerEndpoint(agent), "--status-file="} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %q in args %q", want, joined)
		}
	}
	// The timeout must be bounded and finite: 08 §2.4 has the agent START on expiry, so an
	// unbounded wait would convert a broker outage into a stuck pod — the outcome the
	// observe-and-report mode exists to avoid.
	if !strings.Contains(joined, "--wait-timeout=") {
		t.Errorf("the wait must be bounded (08 §2.4), got %q", joined)
	}
}

func initNames(cs []corev1.Container) []string {
	out := make([]string, 0, len(cs))
	for _, c := range cs {
		out = append(out, c.Name)
	}
	return out
}

// TestTwoAgentsInOneNamespaceRenderDistinctPairs is V-BRK-012's value-level half, and it uses TWO
// CRs because LSN-015 is exactly this mistake: a single-CR render cannot distinguish a name derived
// from the CR from a constant, so every property below holds trivially with one agent and fails
// loudly with two.
//
// Co-location is not hypothetical — 08 §2.6 puts a platform and a cluster-admin agent in
// `kubeagents-system` together, so this is the deployed arrangement, not an edge case.
func TestTwoAgentsInOneNamespaceRenderDistinctPairs(t *testing.T) {
	const ns = "kubeagents-system"
	platform := brokerTestAgent("platform", ns, agentv1alpha1.TierPlatform,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod"})
	clusterAdmin := brokerTestAgent("eu-1-admin", ns, agentv1alpha1.TierClusterAdmin,
		&agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "eu-1"})

	for _, pair := range []struct {
		what string
		a, b string
	}{
		{"broker Deployment name", brokerName(platform), brokerName(clusterAdmin)},
		{"broker TLS secret", brokerTLSSecretName(platform), brokerTLSSecretName(clusterAdmin)},
		{"agent mesh secret", agentMeshTLSSecretName(platform), agentMeshTLSSecretName(clusterAdmin)},
		{"endpoint", brokerEndpoint(platform), brokerEndpoint(clusterAdmin)},
		{"actor ServiceAccount", actorServiceAccountName(platform), actorServiceAccountName(clusterAdmin)},
	} {
		if pair.a == pair.b {
			t.Errorf("%s is shared between two co-located agents: %q", pair.what, pair.a)
		}
	}

	// The selectors must not overlap. If they did, one agent's envelopes would be balanced into
	// the other's broker — and the receiving broker would apply ITS scope, which for the
	// cluster-admin/platform pairing means a namespace-scoped intent evaluated against a
	// project-wide authority.
	svcA, svcB := buildBrokerService(platform), buildBrokerService(clusterAdmin)
	podA := buildBrokerDeployment(platform).Spec.Template.Labels
	podB := buildBrokerDeployment(clusterAdmin).Spec.Template.Labels

	if selectorMatches(svcA.Spec.Selector, podB) {
		t.Error("the platform broker Service selects the cluster-admin broker's pods")
	}
	if selectorMatches(svcB.Spec.Selector, podA) {
		t.Error("the cluster-admin broker Service selects the platform broker's pods")
	}
	if !selectorMatches(svcA.Spec.Selector, podA) || !selectorMatches(svcB.Spec.Selector, podB) {
		t.Error("a broker Service must select its own pods")
	}

	// And no Service may select the READER half. The agent pod has no envelope listener, so half
	// the connections would simply fail — but the reason to assert it is the other direction: a
	// selector loose enough to catch the reader is one that stopped distinguishing the two
	// credentials.
	readerLabels := buildDeployment(platform, "h1", "h2", "h3").Spec.Template.Labels
	if selectorMatches(svcA.Spec.Selector, readerLabels) {
		t.Error("the broker Service selects the agent (reader) pod")
	}
}

func selectorMatches(selector, labels map[string]string) bool {
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}

// TestNoFleetWideBroker is the second clause of V-BRK-012: "no fleet-wide writer exists anywhere".
// Every rendered name must MOVE when the CR name moves. A name that does not is, by definition, a
// singleton that a second agent would share — which is what a fleet-wide writer is.
func TestNoFleetWideBroker(t *testing.T) {
	a := brokerTestAgent("first", "ns", agentv1alpha1.TierPlatform, &agentv1alpha1.ScopeSpec{ProjectID: "p1"})
	b := brokerTestAgent("second", "ns", agentv1alpha1.TierPlatform, &agentv1alpha1.ScopeSpec{ProjectID: "p2"})

	depA, depB := buildBrokerDeployment(a), buildBrokerDeployment(b)
	svcA, svcB := buildBrokerService(a), buildBrokerService(b)

	for _, tc := range []struct {
		what string
		a, b string
	}{
		{"Deployment name", depA.Name, depB.Name},
		{"Service name", svcA.Name, svcB.Name},
		{"pod ServiceAccount", depA.Spec.Template.Spec.ServiceAccountName, depB.Spec.Template.Spec.ServiceAccountName},
		{"Service selector agent label", svcA.Spec.Selector[agentlabels.Agent], svcB.Spec.Selector[agentlabels.Agent]},
	} {
		if tc.a == tc.b {
			t.Errorf("%s does not vary with the Agent CR (%q for both) — that is a fleet-wide writer", tc.what, tc.a)
		}
	}
}
