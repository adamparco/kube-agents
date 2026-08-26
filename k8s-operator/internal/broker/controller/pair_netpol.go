package controller

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

// The pair's own NetworkPolicies (08 §2.7, 05 §1 C1) — P9-T7d-2.
//
// # Why these are controller-rendered and the tier egress policy is not
//
// This repo already has per-tier egress policies, and they are install-time templates rendered by
// `provision_13_apply_network_policies.sh` from `netpol-agent-egress.yaml.template`, with committed
// exemplars under examples/gitops-repo/ held byte-identical by dev/tests/reference-render.py. Those
// select on `kube-agents/tier` and describe where a TIER may reach — a fleet policy decision, made
// by a human, reviewed in a PR.
//
// These two select on `kube-agents/agent` and describe the internal wiring of ONE pair. They cannot
// be install-time artifacts because the CR they key on does not exist at install time, and 08 §2.7
// grants the controller full CRUD on NetworkPolicies for exactly this reason — bounded to "objects
// the controller owns via OwnerReference", which is why both carry one.
//
// # NetworkPolicy is a union, and that shapes what these may claim
//
// Two policies selecting the same pod ADD their allowances; there is no deny rule and no ordering.
// So neither of these can restrict anything the tier policy already permits, and the tier policy
// cannot restrict what these permit. Read them as two holes punched deliberately:
//
//   - `<agent>-broker-ingress` makes the broker default-deny on INGRESS — a direction nothing else
//     in this repo constrains today, so before this policy exists any pod in the namespace can open
//     a connection to :8443. It admits exactly one peer.
//   - `<agent>-to-broker` admits the agent's egress hop to that port, which the tier allowlist does
//     not cover: its in-cluster rule is :80/:8080 into the control namespace (LiteLLM and the token
//     minter), not :8443 to a sibling pod.
//
// # What these do NOT do, stated because the omission is load-bearing
//
// They do not authenticate anything. The broker still requires a client certificate, still
// TokenReviews the projected token, still compares the result to the single `ExpectedCaller` it
// serves, and still refuses on peer mismatch (internal/broker/auth.go). A NetworkPolicy that
// admitted the wrong pod would produce a TLS handshake failure, not an authorized write. These
// exist to make the broker unreachable by default, not to decide who may act.
//
// # The gap this unit found and did not close: the broker's egress to the API server
//
// The broker's whole job needs the kube-apiserver — TokenReview (pipeline step 1), the FleetFreeze
// read (step 5), the ActionRecord write (step 11). The broker pod carries `kube-agents/tier`, so
// the tier egress policy selects it and makes it default-deny on egress, and that allowlist has no
// API-server rule: DNS, the control namespace on :80/:8080, restricted.googleapis.com, and GitHub.
// Nothing there is the API server.
//
// No policy rendered here can fix that. NetworkPolicy cannot name a Service, so "allow the
// `kubernetes` endpoint" is not expressible; it needs the control-plane CIDR, which is per-cluster
// and known only at install time. The hole therefore belongs in the same template as the other
// egress rules, and is **P9-T7d-4**. Whether a packet actually reaches the API server today is an
// L2 question and sits with V-ISO-001/002 in P9-T9 — this comment records the finding so that a
// broker stuck on TokenReview is diagnosed as a missing egress rule rather than as broken auth.

// brokerIngressPolicyName and agentToBrokerPolicyName are the two policies' names. Suffixed rather
// than shared because a single policy cannot express both directions for two different pods: the
// selector is what makes a policy default-deny for the pods it selects, and these select opposite
// halves of the pair.
func brokerIngressPolicyName(agent *agentv1alpha1.Agent) string {
	return agent.Name + "-broker-ingress"
}

func agentToBrokerPolicyName(agent *agentv1alpha1.Agent) string {
	return agent.Name + "-to-broker"
}

// pairPodSelector is the selector both policies use to name one half of one pair, and the
// conjunction is the same one buildBrokerService argues for. `role` alone selects every broker (or
// every agent) in the namespace, and 08 §2.6 co-locates the platform and cluster-admin pairs in
// `kubeagents-system`, so `role: actor` alone would admit a different agent's reader to this
// broker — a scope escape that reads as a routing detail. `agent: <name>` alone selects BOTH halves
// of this pair, which would make the broker's ingress policy admit the broker to itself and, worse,
// make the agent's egress rule a rule about reaching itself.
func pairPodSelector(agent *agentv1alpha1.Agent, role string) metav1.LabelSelector {
	return metav1.LabelSelector{MatchLabels: map[string]string{
		agentlabels.Agent: agent.Name,
		agentlabels.Role:  role,
	}}
}

// brokerEnvelopePort is the port both rules name. Numeric, not the `envelope` named port, even
// though NetworkPolicy accepts a name: named ports resolve through the pod spec and the resolution
// is CNI-implemented rather than API-server-implemented, so a CNI that does not resolve them fails
// OPEN or CLOSED silently depending on which one it is — and LSN-028 is this repo already paying
// for a rule whose semantics differed between dataplanes. The number is single-sourced from
// broker.Port, the same constant the Service and the container port use.
func brokerEnvelopePort() []networkingv1.NetworkPolicyPort {
	proto := corev1.ProtocolTCP
	port := intstr.FromInt32(broker.Port)
	return []networkingv1.NetworkPolicyPort{{Protocol: &proto, Port: &port}}
}

// buildBrokerIngressPolicy makes the broker reachable by its own agent and by nothing else.
//
// There is no `namespaceSelector` on the peer, and that is not an omission: a bare `podSelector`
// peer means "pods matching this selector IN THE POLICY'S OWN NAMESPACE". Adding a
// `namespaceSelector` would widen it to every namespace whose labels match, and the pair is
// co-located by construction (08 §2.6), so the narrower form is also the correct one.
func buildBrokerIngressPolicy(agent *agentv1alpha1.Agent) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      brokerIngressPolicyName(agent),
			Namespace: agent.Namespace,
			Labels:    agentlabels.For(agent, agentlabels.RoleActor),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: pairPodSelector(agent, agentlabels.RoleActor),
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{{
				From: []networkingv1.NetworkPolicyPeer{{
					PodSelector: ptr.To(pairPodSelector(agent, agentlabels.RoleReader)),
				}},
				Ports: brokerEnvelopePort(),
			}},
		},
	}
}

// buildAgentToBrokerPolicy admits the agent's one egress hop to its broker.
//
// Egress only. Giving this policy an `Ingress` policyType as well would make the AGENT pod
// default-deny on ingress as a side effect of describing an egress hop, and the agent's ingress is
// a separate question with a separate owner (the `<agent>` Service fronts it for the router). A
// policy that silently closes a direction it does not mention is the failure mode this comment
// exists to prevent.
func buildAgentToBrokerPolicy(agent *agentv1alpha1.Agent) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agentToBrokerPolicyName(agent),
			Namespace: agent.Namespace,
			Labels:    agentlabels.For(agent, agentlabels.RoleReader),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: pairPodSelector(agent, agentlabels.RoleReader),
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeEgress},
			Egress: []networkingv1.NetworkPolicyEgressRule{{
				To: []networkingv1.NetworkPolicyPeer{{
					PodSelector: ptr.To(pairPodSelector(agent, agentlabels.RoleActor)),
				}},
				Ports: brokerEnvelopePort(),
			}},
		},
	}
}

// reconcilePairNetworkPolicies applies both policies, owner-referenced to the Agent.
//
// Applied BEFORE the Deployments in reconcileWorkloadPair. Ordering matters in one direction only:
// the ingress policy is the one that closes a door, so applying it after the broker is already
// serving leaves a window in which any pod in the namespace can reach :8443. The egress policy
// opens a door and is therefore order-indifferent, but they ship together because a reader that
// cannot reach a broker nothing else can reach either is a pair that never converges.
func (r *AgentReconciler) reconcilePairNetworkPolicies(ctx context.Context, agent *agentv1alpha1.Agent) error {
	for _, np := range []*networkingv1.NetworkPolicy{
		buildBrokerIngressPolicy(agent),
		buildAgentToBrokerPolicy(agent),
	} {
		if err := ctrl.SetControllerReference(agent, np, r.Scheme); err != nil {
			return err
		}
		if err := r.Patch(ctx, np, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller")); err != nil {
			return fmt.Errorf("failed to apply NetworkPolicy %q: %w", np.Name, err)
		}
	}
	return nil
}
