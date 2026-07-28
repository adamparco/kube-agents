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

// mesh_trust.go — the two certificates that let the pair T7b renders actually talk (08 §2.3, §2.7).
//
// T7b mounts `<agent>-broker-tls` and `<agent>-mesh-tls` by name and stops there, deliberately:
// mutual TLS needs ONE certificate authority signing both ends, and until this file there was no
// such authority in the repository. The only `Issuer` here was the namespaced `selfsigned-issuer`
// the webhook uses, and self-signing each end separately produces two certificates that each
// verify against themselves and neither against the other — a pair that fails the exact handshake
// it exists to perform, with an error message about unknown authorities rather than about
// configuration.
//
// # The controller creates Certificates and never touches a Secret
//
// 08 §2.7 withholds `get`/`list`/`watch` on Secrets from this controller entirely — not as caution
// but because a list verb in a namespace hosting an agent hands the controller every projected
// token and Workload-Identity Secret in that namespace, which is the actor credentials themselves.
// So the controller asks cert-manager for a `Certificate` and cert-manager writes the Secret. The
// controller never reads the result back; it learns whether the certificate exists the same way an
// operator does, by whether the broker pod started (`BrokerReady`).
//
// That is also why the private keys are never in a Go variable here. There is no code path in this
// process that holds mesh key material, so there is no code path that can log it.
//
// # Why the CA is a ClusterIssuer, and why that is not a shortcut
//
// Agents are per-namespace and the mesh spans all of them. A namespaced `Issuer` would need the CA
// Secret present in every namespace hosting an agent — which puts the mesh CA's PRIVATE KEY in
// every tenant namespace, where the tenant's own pods can be granted read access to it by the
// tenant's own RBAC. One compromised namespace would then mint a valid identity for any workload
// in the fleet. A `ClusterIssuer` keeps the key in cert-manager's namespace and hands out only
// leaves. The CA itself is NOT created here: it is install-time, cluster-scoped, and a
// trust-bootstrap decision, so it ships as static manifests under `config/mesh-ca/` applied by the
// provisioning path. This controller holds no cluster-scoped write verb and must not gain one.
//
// # The fleet-wide CA is not a fleet-wide authorization
//
// Worth stating because it looks wrong at first glance: one CA signs every agent's client
// certificate, so agent A's certificate verifies against agent B's broker. That would be an
// isolation hole if the certificate were the authorization, and it is not. `internal/broker/auth.go`
// authenticates the CALLER with a TokenReview against a projected token whose audience is
// `kubeagents-broker`, compares the result against `ExpectedCaller` — singular, the one reader this
// broker serves — and then binds the two layers by requiring the client certificate's SPIFFE URI to
// equal the ID derived from the token. Agent A presenting its own valid certificate to B's broker
// gets `ForbiddenCaller`; presenting A's certificate with a token stolen from B gets
// `PeerMismatch`. The certificate proves membership of the mesh; the token proves which member.
//
// The consequence for this file is a hard requirement rather than a nicety: the agent's certificate
// MUST carry the SPIFFE URI SAN the broker will derive, or every envelope is refused at the
// transport layer. That string comes from `broker.SPIFFEID`, the same function `auth.go` calls.

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

const (
	// meshCAIssuerName is the ClusterIssuer created by config/mesh-ca/. A constant, and unlike
	// everything in broker_manifests.go it is CORRECT for this one to be a constant: the CA is the
	// thing every agent must share, so a per-agent value here would give each pair its own trust
	// root and reproduce exactly the failure this file exists to fix.
	meshCAIssuerName = "kubeagents-mesh-ca"

	// certManagerGroup / certManagerVersion identify the API the controller renders into.
	//
	// Rendered as `unstructured` rather than by importing cert-manager's Go types, and that is a
	// supply-chain decision with a measured cost: adding the module upgrades every `k8s.io/*`
	// dependency in this operator and pulls `sigs.k8s.io/gateway-api` — an unrelated API surface —
	// into the binary that reconciles the write-credential path. Two struct literals do not justify
	// that. The controller only ever WRITES these objects (it holds no read verb it needs here), so
	// the type safety being traded away is type safety over a value nothing in this process reads.
	certManagerGroup   = "cert-manager.io"
	certManagerVersion = "v1"

	// meshCertDuration / meshCertRenewBefore. Ninety days with a fifteen-day renewal window.
	//
	// Short for a CA-issued leaf, on purpose: these certificates are the mesh's membership proof,
	// and the revocation story for a cert-manager leaf is "wait for it to expire" — there is no CRL
	// and no OCSP responder in this design. The expiry window IS the revocation window, so it is
	// set to something an incident can outlive rather than something an audit cycle can.
	meshCertDuration    = "2160h" // 90d
	meshCertRenewBefore = "360h"  // 15d
)

// certificateGVK is the object kind this file renders.
func certificateGVK() schema.GroupVersionKind {
	return schema.GroupVersionKind{Group: certManagerGroup, Version: certManagerVersion, Kind: "Certificate"}
}

// buildBrokerCertificate is the SERVER half: what the agent connects to and pins.
//
// `dnsNames` carries the broker's in-cluster FQDN because that is what the agent's TLS client sets
// as its ServerName — `brokerSAN`, the same derivation the `wait-for-broker` init container is
// given, so the name asserted and the name presented cannot drift.
//
// It also carries the actor's SPIFFE URI. Nothing verifies it today: the agent pins the DNS name
// and stops. It is here because a certificate is the wrong thing to add an identity to later — the
// leaves in the field at that moment would all lack it, and the check that consumed it would have
// to be written to tolerate its absence, which is the same as not having it.
func buildBrokerCertificate(agent *agentv1alpha1.Agent) *unstructured.Unstructured {
	return meshCertificate(agent, meshCertificateSpec{
		name:       brokerName(agent),
		secretName: brokerTLSSecretName(agent),
		role:       agentlabels.RoleActor,
		dnsNames:   []string{brokerSAN(agent)},
		spiffe:     brokerSPIFFEID(agent),
		// `server auth` first because that is what this certificate is FOR. `client auth` is not
		// granted: the broker's outbound calls are to the API server, which authenticates it by
		// ServiceAccount token, not by this keypair. A certificate usable as a client credential
		// in a mesh where client certificates are half of the identity check is authority nobody
		// asked for.
		usages: []any{"server auth", "digital signature", "key encipherment"},
	})
}

// buildAgentMeshCertificate is the CLIENT half, and its SPIFFE URI is load-bearing.
//
// `broker.SPIFFEID` is called here and in `auth.go`'s peer-mismatch check. If this certificate
// carried a URI of any other shape, the broker would refuse every envelope with `PeerMismatch` —
// a fleet-wide outage whose error text is about trust domains, discoverable only at L2, after a
// rollout. One function, two callers, no format string in this file.
//
// No `dnsNames`. Nothing dials the agent on this keypair; it is presented, never listened on, and
// a DNS SAN would be a name the certificate vouches for that no verifier ever checks.
func buildAgentMeshCertificate(agent *agentv1alpha1.Agent) *unstructured.Unstructured {
	return meshCertificate(agent, meshCertificateSpec{
		name:       agent.Name + "-mesh",
		secretName: agentMeshTLSSecretName(agent),
		role:       agentlabels.RoleReader,
		spiffe:     readerSPIFFEID(agent),
		usages:     []any{"client auth", "digital signature", "key encipherment"},
	})
}

// readerSPIFFEID / brokerSPIFFEID are the two mesh identities of one pair.
//
// Both go through broker.SPIFFEID — see the comment there. Both name a ServiceAccount that this
// controller does not create: the reader SA is pre-created via GitOps (P1-T4/T5, 08 §4) and the
// actor SA likewise. The certificate asserts an identity; RBAC decides what that identity may do.
// Keeping those two in different hands is the reason 06 §2.2.1 can say the agent cannot choose its
// own authority level.
func readerSPIFFEID(agent *agentv1alpha1.Agent) string {
	return broker.SPIFFEID(broker.DefaultTrustDomain, agent.Namespace, readerServiceAccountName(agent))
}

func brokerSPIFFEID(agent *agentv1alpha1.Agent) string {
	return broker.SPIFFEID(broker.DefaultTrustDomain, agent.Namespace, actorServiceAccountName(agent))
}

// meshCertificateSpec is the per-end input to the one renderer below.
type meshCertificateSpec struct {
	name       string
	secretName string
	role       string
	dnsNames   []string
	spiffe     string
	usages     []any
}

// meshCertificate renders one end. ONE renderer for both, so the properties that must hold for the
// pair — same issuer, same key policy, same lifetime — are structurally shared rather than
// duplicated and kept in step by review.
func meshCertificate(agent *agentv1alpha1.Agent, c meshCertificateSpec) *unstructured.Unstructured {
	spec := map[string]any{
		"secretName":  c.secretName,
		"duration":    meshCertDuration,
		"renewBefore": meshCertRenewBefore,
		"uris":        []any{c.spiffe},
		"usages":      c.usages,
		"privateKey": map[string]any{
			"algorithm": "ECDSA",
			"size":      int64(256),
			// Without this cert-manager REUSES the existing private key on renewal, so a rotation
			// rotates the certificate and not the secret it is protecting. A renewal that cannot
			// recover from key disclosure is a calendar event, not a control.
			"rotationPolicy": "Always",
		},
		"issuerRef": map[string]any{
			"kind":  "ClusterIssuer",
			"name":  meshCAIssuerName,
			"group": certManagerGroup,
		},
	}
	if len(c.dnsNames) > 0 {
		dns := make([]any, 0, len(c.dnsNames))
		for _, n := range c.dnsNames {
			dns = append(dns, n)
		}
		spec["dnsNames"] = dns
	}

	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": certManagerGroup + "/" + certManagerVersion,
		"kind":       "Certificate",
		"metadata": map[string]any{
			"name":      c.name,
			"namespace": agent.Namespace,
			"labels":    toAnyMap(agentlabels.For(agent, c.role)),
		},
		"spec": spec,
	}}
	obj.SetGroupVersionKind(certificateGVK())
	return obj
}

// toAnyMap converts a label map for embedding in an Unstructured. `map[string]string` survives a
// round trip through the API server but not through `unstructured`'s deep-copy, which requires
// every value to be a JSON type.
func toAnyMap(in map[string]string) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// reconcileMeshCertificates applies both ends, or neither.
//
// cert-manager is optional in this design — 08 §2.7 says "where cert-manager is present" — so a
// cluster without the CRD must not produce a reconcile error loop on every Agent. It produces one
// log line and a pair that stays `BrokerReady: false`, which is the honest report: the workloads
// are rendered, the trust material is not, and nothing is pretending otherwise.
//
// The distinction matters for what it refuses to do. The tempting alternative is to fall back to a
// self-signed certificate per end so the pods at least start. That would give a broker serving TLS
// that no agent can verify, and the failure would surface as a handshake error inside the init
// container's observe-and-report path — indistinguishable from a broker that is merely slow. Not
// having a certificate is a legible state; having the wrong one is not.
func (r *AgentReconciler) reconcileMeshCertificates(ctx context.Context, agent *agentv1alpha1.Agent) error {
	log := logf.FromContext(ctx)

	if _, err := r.RESTMapper().RESTMapping(certificateGVK().GroupKind(), certificateGVK().Version); err != nil {
		log.Info("cert-manager is not installed; skipping mesh certificates. The broker will stay "+
			"NotReady until <agent>-broker-tls and <agent>-mesh-tls exist (08 §2.3)",
			"agent", agent.Name, "namespace", agent.Namespace, "reason", err.Error())
		return nil
	}

	for _, cert := range []*unstructured.Unstructured{
		buildBrokerCertificate(agent),
		buildAgentMeshCertificate(agent),
	} {
		// Owner-referenced so deleting the Agent deletes the Certificate, and cert-manager's own
		// ownership of the Secret deletes that in turn. The controller therefore causes the mesh
		// key material to be destroyed without ever being able to read it.
		if err := controllerutil.SetControllerReference(agent, cert, r.Scheme); err != nil {
			return fmt.Errorf("failed to own Certificate %q: %w", cert.GetName(), err)
		}
		if err := r.Patch(ctx, cert, client.Apply, client.ForceOwnership, client.FieldOwner("agent-controller")); err != nil {
			return fmt.Errorf("failed to apply Certificate %q: %w", cert.GetName(), err)
		}
	}
	return nil
}
