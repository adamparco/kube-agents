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
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/util/intstr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

// The pair's NetworkPolicies (08 §2.7, P9-T7d-2) at L1.
//
// V-ISO-001/002 ask whether a packet is DROPPED, which is L2 and belongs to P9-T9. Nothing here
// claims that. What these tests can claim is the part a dataplane cannot tell you apart from a
// misconfigured one: that the selectors name the pods they were meant to name. A policy whose
// selector matches nothing also drops every packet, and at L2 it looks exactly like a policy that
// is working.

func netpolAgent(name, ns string) *agentv1alpha1.Agent {
	a := pausableAgent(nil)
	a.Name = name
	a.Namespace = ns
	a.Spec.Scope = &agentv1alpha1.ScopeSpec{ProjectID: "p", ClusterName: "c", Namespace: ns}
	return a
}

// selects reports whether sel matches the label set of the given half of the given agent, using the
// same matcher the API server uses. Written against agentlabels.For -- the function that actually
// stamps the Deployments -- rather than against a hand-built map, because a selector test that
// builds its own idea of the pod's labels is testing the test.
func selects(t *testing.T, sel metav1.LabelSelector, agent *agentv1alpha1.Agent, role string) bool {
	t.Helper()
	s, err := metav1.LabelSelectorAsSelector(&sel)
	if err != nil {
		t.Fatalf("selector %v is not valid: %v", sel, err)
	}
	return s.Matches(labels.Set(agentlabels.For(agent, role)))
}

// TestBrokerIngressAdmitsItsOwnReaderAndNothingElse is the load-bearing test in this file.
//
// Four cases, and the two negatives are the ones that matter. The broker's Service selector already
// argues (buildBrokerService) that `role: actor` alone selects every broker in the namespace,
// because 08 §2.6 co-locates the platform and cluster-admin pairs in kubeagents-system. The same
// asymmetry applies here in the other direction: a peer selector of `role: reader` alone would
// admit ANOTHER agent's reader to this broker. That reader would then be refused by the broker's
// own TokenReview -- so the consequence is not an unauthorized write -- but the policy would have
// stopped being the boundary it is documented as being, and the only signal would be a rise in
// PeerMismatch refusals that reads as a certificate problem.
func TestBrokerIngressAdmitsItsOwnReaderAndNothingElse(t *testing.T) {
	mine := netpolAgent("alpha", "kubeagents-system")
	theirs := netpolAgent("beta", "kubeagents-system")

	np := buildBrokerIngressPolicy(mine)

	if got := len(np.Spec.Ingress); got != 1 {
		t.Fatalf("broker ingress policy has %d rules, want exactly 1 -- a second rule is a second door", got)
	}
	if got := len(np.Spec.Ingress[0].From); got != 1 {
		t.Fatalf("the ingress rule has %d peers, want exactly 1", got)
	}
	peer := np.Spec.Ingress[0].From[0]
	if peer.PodSelector == nil {
		t.Fatal("the peer has no podSelector, which admits every pod the rule's other fields allow")
	}
	if peer.NamespaceSelector != nil {
		t.Errorf("the peer carries a namespaceSelector (%v); a bare podSelector already means "+
			"'this namespace', and adding one can only widen it", peer.NamespaceSelector)
	}
	if peer.IPBlock != nil {
		t.Errorf("the peer carries an ipBlock (%v); pod IPs are not identities", peer.IPBlock)
	}

	for _, tc := range []struct {
		what  string
		agent *agentv1alpha1.Agent
		role  string
		want  bool
	}{
		{"its own reader", mine, agentlabels.RoleReader, true},
		{"its own broker (itself)", mine, agentlabels.RoleActor, false},
		{"another agent's reader", theirs, agentlabels.RoleReader, false},
		{"another agent's broker", theirs, agentlabels.RoleActor, false},
	} {
		if got := selects(t, *peer.PodSelector, tc.agent, tc.role); got != tc.want {
			t.Errorf("broker ingress peer selects %s = %v, want %v", tc.what, got, tc.want)
		}
	}

	// And the policy must select the broker it is protecting, or it protects nothing while looking
	// like it does. This is the LSN-035 arm: an empty selector set is the state in which every
	// assertion above still passes.
	if !selects(t, np.Spec.PodSelector, mine, agentlabels.RoleActor) {
		t.Error("the policy does not select its own broker, so it constrains no pod at all")
	}
	if selects(t, np.Spec.PodSelector, mine, agentlabels.RoleReader) {
		t.Error("the policy selects the AGENT pod, which would make the agent default-deny on ingress " +
			"as a side effect -- the router could no longer reach it")
	}
	if selects(t, np.Spec.PodSelector, theirs, agentlabels.RoleActor) {
		t.Error("the policy selects another agent's broker; one CR's controller-owned policy would " +
			"then govern a pod it does not own")
	}
}

// TestBrokerIngressIsIngressOnly. Adding Egress to policyTypes would make the broker default-deny
// on EGRESS with an empty egress rule list -- i.e. no egress at all -- which takes out TokenReview,
// the FleetFreeze read and the journal write in one line. That failure surfaces as a broker that
// authenticates nobody, which reads as an auth bug.
func TestBrokerIngressIsIngressOnly(t *testing.T) {
	np := buildBrokerIngressPolicy(netpolAgent("alpha", "kubeagents-system"))
	if want := []networkingv1.PolicyType{networkingv1.PolicyTypeIngress}; !equalPolicyTypes(np.Spec.PolicyTypes, want) {
		t.Errorf("policyTypes = %v, want %v", np.Spec.PolicyTypes, want)
	}
	if len(np.Spec.Egress) != 0 {
		t.Errorf("the broker ingress policy declares %d egress rules; it has no business having any", len(np.Spec.Egress))
	}
}

// TestAgentEgressPolicyIsEgressOnlyAndPointsAtItsOwnBroker is the mirror. The agent's ingress is a
// separate question with a separate owner -- the `<agent>` Service fronts it for the router -- so a
// policy that describes an egress hop must not close a direction it does not mention.
func TestAgentEgressPolicyIsEgressOnlyAndPointsAtItsOwnBroker(t *testing.T) {
	mine := netpolAgent("alpha", "kubeagents-system")
	theirs := netpolAgent("beta", "kubeagents-system")

	np := buildAgentToBrokerPolicy(mine)

	if want := []networkingv1.PolicyType{networkingv1.PolicyTypeEgress}; !equalPolicyTypes(np.Spec.PolicyTypes, want) {
		t.Errorf("policyTypes = %v, want %v", np.Spec.PolicyTypes, want)
	}
	if len(np.Spec.Ingress) != 0 {
		t.Errorf("the agent egress policy declares %d ingress rules, which would close the router's path in", len(np.Spec.Ingress))
	}
	if !selects(t, np.Spec.PodSelector, mine, agentlabels.RoleReader) {
		t.Error("the policy does not select its own agent pod")
	}
	if selects(t, np.Spec.PodSelector, mine, agentlabels.RoleActor) {
		t.Error("the policy selects the BROKER pod; the broker's egress is governed elsewhere and " +
			"an egress policy naming only :8443-to-itself would be a broker with no API server")
	}

	if got := len(np.Spec.Egress); got != 1 {
		t.Fatalf("agent egress policy has %d rules, want exactly 1", got)
	}
	if got := len(np.Spec.Egress[0].To); got != 1 {
		t.Fatalf("the egress rule has %d peers, want exactly 1", got)
	}
	peer := np.Spec.Egress[0].To[0]
	if peer.PodSelector == nil {
		t.Fatal("the peer has no podSelector")
	}
	if !selects(t, *peer.PodSelector, mine, agentlabels.RoleActor) {
		t.Error("the agent's egress rule does not reach its own broker, so the hop it exists to permit is not permitted")
	}
	if selects(t, *peer.PodSelector, theirs, agentlabels.RoleActor) {
		t.Error("the agent's egress rule reaches ANOTHER agent's broker")
	}
}

// TestBothPoliciesNameTheSamePortTheBrokerListensOn. The two rules and the Service and the container
// port are four spellings of one number; three of them agreeing while the fourth drifts produces a
// connection that is refused rather than dropped, which does not look like a policy problem at all.
func TestBothPoliciesNameTheSamePortTheBrokerListensOn(t *testing.T) {
	agent := netpolAgent("alpha", "kubeagents-system")

	for name, ports := range map[string][]networkingv1.NetworkPolicyPort{
		"broker ingress": buildBrokerIngressPolicy(agent).Spec.Ingress[0].Ports,
		"agent egress":   buildAgentToBrokerPolicy(agent).Spec.Egress[0].Ports,
	} {
		if len(ports) != 1 {
			t.Errorf("%s names %d ports, want exactly 1", name, len(ports))
			continue
		}
		p := ports[0]
		if p.Protocol == nil || *p.Protocol != corev1.ProtocolTCP {
			t.Errorf("%s does not pin protocol TCP (got %v); an unset protocol defaults to TCP but "+
				"says nothing about intent", name, p.Protocol)
		}
		if p.Port == nil {
			t.Errorf("%s leaves the port unset, which admits EVERY port on the peer", name)
			continue
		}
		// Numeric, not the `envelope` named port -- see brokerEnvelopePort for why. Asserting the
		// type as well as the value is what keeps that decision from being quietly reverted.
		if p.Port.Type != intstr.Int {
			t.Errorf("%s names the port by string (%q); named-port resolution in NetworkPolicy is "+
				"CNI-implemented, and LSN-028 is this repo already paying for a rule whose semantics "+
				"differed between dataplanes", name, p.Port.StrVal)
		}
		if p.Port.IntValue() != int(broker.Port) {
			t.Errorf("%s names port %d, but the broker listens on %d", name, p.Port.IntValue(), broker.Port)
		}
	}
}

// TestPairPoliciesAreNamedAndScopedPerAgent renders TWO CRs, per LSN-015: every derivation in this
// file is per-CR, and a one-agent fixture cannot fail a collision assertion.
//
// The names collide only if `agent.Name` collides, which the API server prevents within a
// namespace -- unlike the actor SPIFFE ID, whose uniqueness rests on admission (see
// mesh_trust_test.go). Asserted anyway, because the failure mode is that one agent's
// controller-owned policy is adopted and overwritten by another's reconcile loop, and server-side
// apply would do that silently.
func TestPairPoliciesAreNamedAndScopedPerAgent(t *testing.T) {
	a := netpolAgent("alpha", "kubeagents-system")
	b := netpolAgent("beta", "kubeagents-system")

	seen := map[string]string{}
	for _, agent := range []*agentv1alpha1.Agent{a, b} {
		for _, np := range []*networkingv1.NetworkPolicy{
			buildBrokerIngressPolicy(agent),
			buildAgentToBrokerPolicy(agent),
		} {
			if np.Namespace != agent.Namespace {
				t.Errorf("policy %q lands in %q, not the agent's namespace %q -- a NetworkPolicy only "+
					"governs pods in its own namespace, so this one governs nothing",
					np.Name, np.Namespace, agent.Namespace)
			}
			if prev, dup := seen[np.Name]; dup {
				t.Errorf("policy name %q is claimed by both %s and %s", np.Name, prev, agent.Name)
			}
			seen[np.Name] = agent.Name

			if np.Labels[agentlabels.Agent] != agent.Name {
				t.Errorf("policy %q carries %s=%q, want %q", np.Name,
					agentlabels.Agent, np.Labels[agentlabels.Agent], agent.Name)
			}
		}
	}
	if len(seen) != 4 {
		t.Errorf("two agents produced %d distinct policy names, want 4", len(seen))
	}
}

// TestPairPoliciesTargetThePodsTheLauncherActuallyRenders closes the loop against the real workload
// render rather than against agentlabels.For.
//
// This is the non-vacuity arm (LSN-035) for the whole file. Every other test here compares one
// selector against one label map, and both come from the same function -- so all of them would keep
// passing if BuildPair stopped stamping `kube-agents/agent` tomorrow. This one asks the renderer.
func TestPairPoliciesTargetThePodsTheLauncherActuallyRenders(t *testing.T) {
	agent := netpolAgent("alpha", "kubeagents-system")
	pair := (&nativePodLauncher{}).BuildPair(agent, "cfg", "fb", "set")

	ingress := buildBrokerIngressPolicy(agent)
	egress := buildAgentToBrokerPolicy(agent)

	brokerSel, err := metav1.LabelSelectorAsSelector(&ingress.Spec.PodSelector)
	if err != nil {
		t.Fatalf("broker ingress podSelector invalid: %v", err)
	}
	agentSel, err := metav1.LabelSelectorAsSelector(&egress.Spec.PodSelector)
	if err != nil {
		t.Fatalf("agent egress podSelector invalid: %v", err)
	}

	var matchedBroker, matchedAgent int
	for _, dep := range pair.Ordered() {
		podLabels := labels.Set(dep.Spec.Template.Labels)
		if brokerSel.Matches(podLabels) {
			matchedBroker++
		}
		if agentSel.Matches(podLabels) {
			matchedAgent++
		}
	}
	if matchedBroker != 1 {
		t.Errorf("the broker ingress policy selects %d of the pair's %d rendered pod templates, want exactly 1",
			matchedBroker, len(pair.Ordered()))
	}
	if matchedAgent != 1 {
		t.Errorf("the agent egress policy selects %d of the pair's %d rendered pod templates, want exactly 1",
			matchedAgent, len(pair.Ordered()))
	}
}

func equalPolicyTypes(got, want []networkingv1.PolicyType) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
