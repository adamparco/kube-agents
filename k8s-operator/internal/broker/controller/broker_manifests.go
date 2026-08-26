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

// broker_manifests.go — the second half of the workload pair (08 §2.1, §2.3, §2.6).
//
// One `Agent` CR renders TWO workloads with two ServiceAccounts: the agent pod, which holds the
// model and no write verb, and the broker pod, which holds the write credential and no model.
// 08 §2.2 is blunt about why they cannot be one pod with two containers: a pod has exactly one
// `spec.serviceAccountName` and every container shares it, so a sidecar broker would either be
// unable to write or would hand the LLM's container the actor token. Containers are not a
// credential boundary. Separate pods are.
//
// # Everything here is named from the CR, and that is a security property
//
// V-BRK-012 is "one broker per `Agent` CR; no fleet-wide writer exists anywhere". It reads like a
// deployment-topology preference and is not one: 03 §3.1 bounds the blast radius of a broker
// compromise at exactly one scope, and that bound is only true if there is no second broker with a
// wider one. A single shared broker serving the fleet would be a strictly more convenient design —
// one Deployment, one certificate, one rollout — and it would silently make the blast radius the
// union of every scope in the cluster. So every name below derives from `agent.Name`, there is no
// constant anywhere in this file that could name a singleton, and `dev/tests/one-broker-per-agent.py`
// fails the build if one appears.
//
// # What this file does NOT render, and where it went
//
// The TLS material. 08 §2.3 has both ends present per-agent certificates from the cluster's issuer,
// and 08 §2.7 has the controller create a cert-manager `Certificate` (and touch no Secret at all)
// where cert-manager is present. That needs the mesh CA to exist first, and standing up a CA is a
// trust-bootstrap decision, not a rendering one — it ships in P9-T7d with the actor ServiceAccounts
// and the pair's NetworkPolicies. This file mounts the two Secrets BY NAME and nothing more.
//
// Until T7d lands, those Secrets do not exist and the broker pod stays in `ContainerCreating`. That
// is the correct failure: a missing certificate must not degrade into a broker that serves the
// envelope route in plaintext, and `BrokerReady` stays false with the reason visible on the CR.

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

const (
	// defaultBrokerImage is the fallback image for the broker. ONE image for all three tiers, not
	// one per tier (08 §2.1): the broker is the highest-value credential holder in the system, so
	// it runs on the smallest supply chain in the system — no model, no chat surface, no plugin
	// loader. Which tier it is serving arrives as flags from this renderer, never from a build.
	defaultBrokerImage = "ghcr.io/gke-labs/kube-agents/kage-broker:v0.1.0"

	// brokerImageEnvVar overrides that default on the CONTROLLER's Deployment — see brokerImage for
	// why this is not a CR field.
	brokerImageEnvVar = "KUBEAGENTS_BROKER_IMAGE"

	// brokerPortName is the Service port name of 08 §2.1. `envelope`, not `https`: the agent's own
	// mesh port (8444) is also TLS, and a NetworkPolicy or Service that named them both `https`
	// would read as one surface.
	brokerPortName = "envelope"

	// brokerTLSMountPath matches the defaults in cmd/broker/main.go (`--tls-cert-file` and
	// friends). Named here as a constant that the broker-flag test compares against those defaults,
	// so a change to either side is a red test rather than a crash-looping pod.
	brokerTLSMountPath = "/etc/kage/tls"

	// agentBrokerTokenMountPath is where the AGENT pod finds its projected token for the broker.
	// Deliberately not under /var/run/secrets/kubernetes.io — that path is the default API-server
	// token's, and a second token there is one substring away from being read by something that
	// meant the first.
	agentBrokerTokenMountPath = "/var/run/secrets/kubeagents.x-k8s.io/broker"

	// agentBrokerStatusMountPath is the emptyDir the `wait-for-broker` init container writes its
	// verdict into and the agent container reads. It is a file rather than an exit code because
	// 08 §2.4 requires the init container to succeed EITHER WAY — see buildWaitForBrokerContainer.
	agentBrokerStatusMountPath = "/var/run/kage"

	// brokerTokenExpirationSeconds is the projected token's TTL. One hour, and the kubelet rotates
	// it at 80% of that. Short because this token is the agent pod's only credential that is
	// accepted anywhere with write authority behind it, even though the token itself carries none.
	brokerTokenExpirationSeconds = int64(3600)

	// waitForBrokerTimeoutSeconds bounds the init container's poll (08 §2.4). Long enough for a
	// cold broker image pull, short enough that a permanently-absent broker does not look like a
	// stuck pod: after this the agent starts anyway, in observe-and-report mode.
	waitForBrokerTimeoutSeconds = 120

	// actorSANameLimit and actorSALeafLimit implement the 06 §5.1 truncation rule.
	actorSANameLimit = 253
	actorSALeafLimit = 40

	// brokerReplicas is the broker's replica count. A `const`, which is the point: a constant
	// cannot be derived from an Agent, so this site is structurally incapable of becoming a place
	// where `spec.operations.paused` turns into `replicas: 0` — the same argument V-RUN-012 makes
	// about the agent's replica decider not being handed the brake, made here by other means
	// because there is no decider to constrain. Two brokers would each hold the same actor
	// credential and each keep their own anti-replay window, so a nonce spent on one is unspent on
	// the other; 06 §4.1's single-use guarantee is per-process state.
	brokerReplicas int32 = 1
)

// Volume and mount names. Collected because three functions in two files spell each of them and a
// mount naming a volume that does not exist is a pod the API server accepts and the kubelet then
// refuses, with the error on the Pod and not on the Deployment anyone is looking at.
const (
	brokerTLSVolumeName        = "broker-tls"
	agentMeshTLSVolumeName     = "agent-mesh-tls"
	agentBrokerTokenVolumeName = "broker-token"
	agentBrokerStatusVolume    = "broker-status"
	brokerTmpVolumeName        = "broker-tmp"
)

// brokerName is the broker's Deployment AND Service name. 08 §2.1 fixes the canonical names and
// notes the asymmetry deliberately: the agent's Deployment (`<agent>-gateway`) and Service
// (`<agent>`) differ, while the broker's two share `<agent>-broker`.
func brokerName(agent *agentv1alpha1.Agent) string { return agent.Name + "-broker" }

// brokerTLSSecretName / agentMeshTLSSecretName name the two halves of the mesh keypair. Both are
// created by the trust path (P9-T7d), never by this file, and never read by the controller —
// 08 §2.7 withholds `get`/`list`/`watch` on Secrets from the controller entirely, because a list
// verb in an agent's namespace would hand it every projected token in that namespace.
func brokerTLSSecretName(agent *agentv1alpha1.Agent) string { return agent.Name + "-broker-tls" }
func agentMeshTLSSecretName(agent *agentv1alpha1.Agent) string {
	return agent.Name + "-mesh-tls"
}

// brokerEndpoint is the value injected as KUBEAGENTS_BROKER_ENDPOINT (08 §2.3).
//
// Fully qualified with the cluster suffix on purpose. A short `<svc>.<ns>` name resolves through
// the pod's search-domain list, and search-path resolution is the one part of this address an
// attacker inside the namespace can influence; the FQDN takes the search path out of the question.
func brokerEndpoint(agent *agentv1alpha1.Agent) string {
	return fmt.Sprintf("https://%s.%s.svc.cluster.local:%d", brokerName(agent), agent.Namespace, broker.Port)
}

// brokerSAN is the name the agent pins on the broker's certificate (08 §2.3). Same host as the
// endpoint, minus scheme and port, so the two cannot drift.
func brokerSAN(agent *agentv1alpha1.Agent) string {
	return fmt.Sprintf("%s.%s.svc.cluster.local", brokerName(agent), agent.Namespace)
}

// actorServiceAccountName derives the broker's identity from tier + scope ALONE (06 §5.1).
//
// The CR requests nothing here, and the CRD has no field that could. 06 §2.2.1 states the reason
// in one line: the ability to name the actor identity is the ability to choose an authority level.
// So this is a pure function of two immutable-ish facts about the agent, and the resolved value is
// published in `status.broker.actorServiceAccount` — status, not spec, observable but not settable.
//
// The truncation arm is 06 §5.1's: over 253 characters, the leaf is cut to 40 and suffixed with the
// first 8 hex digits of the sha256 of the full name. Unlike agentlabels.RenderScope this makes no
// injectivity claim beyond that digest — it does not need to, because an SA name is resolved by the
// API server as a literal and a collision here is a pod that binds the wrong identity and FAILS to
// start (no such SA, or an SA with the wrong Role), not one that silently widens.
// An empty leaf is reachable and is left alone deliberately. Admission requires no scope of the
// platform tier (06 §1.2 V-2, and agent_webhook.validateScopeAndParent says so in as many words),
// so a scope-less platform agent renders `platform--actor`. Two things make that safe rather than
// merely tolerable: admission also enforces (tier, scope) uniqueness fleet-wide, so the name still
// belongs to exactly one agent; and the broker's own validate() refuses to start with an empty
// --scope, so such a pair fails closed with BrokerReady false rather than running under-specified.
// Substituting a placeholder here would trade a visible oddity for a name that looks deliberate.
func actorServiceAccountName(agent *agentv1alpha1.Agent) string {
	if agent == nil {
		return ""
	}
	tier := string(agentindex.EffectiveTier(agent))
	leaf := scope.Of(agent).Leaf()
	name := fmt.Sprintf("%s-%s-actor", tier, leaf)
	if len(name) <= actorSANameLimit {
		return name
	}
	sum := sha256.Sum256([]byte(name))
	if len(leaf) > actorSALeafLimit {
		leaf = leaf[:actorSALeafLimit]
	}
	return fmt.Sprintf("%s-%s-%s-actor", tier, leaf, hex.EncodeToString(sum[:])[:8])
}

// buildBrokerService is the `<agent>-broker` ClusterIP Service on 8443 (08 §2.3).
//
// The selector is `role: actor` AND `agent: <cr-name>`, both of them, and that conjunction is the
// point. `role: actor` alone would select every broker in the namespace — the co-location rule
// (08 §2.6) puts a platform and a cluster-admin broker in `kubeagents-system` together — so one
// agent's envelopes would round-robin into another agent's broker, which is a scope escape that
// looks like a load-balancing decision. `agent: <name>` alone would select the agent pod as well,
// which is worse: half the connections would reach a pod with no envelope listener at all.
func buildBrokerService(agent *agentv1alpha1.Agent) *corev1.Service {
	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      brokerName(agent),
			Namespace: agent.Namespace,
			Labels:    agentlabels.For(agent, agentlabels.RoleActor),
		},
		Spec: corev1.ServiceSpec{
			Selector: map[string]string{
				agentlabels.Role:  agentlabels.RoleActor,
				agentlabels.Agent: agent.Name,
			},
			Ports: []corev1.ServicePort{{
				Name:       brokerPortName,
				Port:       broker.Port,
				Protocol:   corev1.ProtocolTCP,
				TargetPort: intstr.FromString(brokerPortName),
			}},
		},
	}
}

// buildBrokerDeployment renders the broker workload (08 §2.1, §2.6).
//
// Read the SecurityContext block as the specification it is, not as boilerplate. This is the one
// pod in the mesh whose ServiceAccount can write, so 08 §2.6 gives it a strictly harder posture
// than the agent's: read-only root filesystem, no shell in the image, and no volume mounts other
// than its certificate and its token. `kubectl exec` into this pod gets a failed exec.
func buildBrokerDeployment(agent *agentv1alpha1.Agent) *appsv1.Deployment {
	labels := agentlabels.For(agent, agentlabels.RoleActor)

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      brokerName(agent),
			Namespace: agent.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			// One replica, and not a knob — see brokerReplicas. A `const` rather than a literal
			// so that V-RUN-012's L0 check can pin the spelling: the second workload in this
			// package is the obvious place for someone to implement `pause` as a scale-to-zero,
			// and killing the broker looks like a tidier pause than killing the agent right up
			// until you notice the agent can no longer say why it is refusing (08 §2.4).
			Replicas: ptr.To(brokerReplicas),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{
				agentlabels.Role:  agentlabels.RoleActor,
				agentlabels.Agent: agent.Name,
			}},
			// Recreate, not RollingUpdate. During a rolling update two brokers run at once, which
			// is the state the replica comment above rules out; a few seconds of refused envelopes
			// is the correct trade, and the agent handles them as an outage it already has a mode
			// for (08 §2.4, observe-and-report).
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					ServiceAccountName: actorServiceAccountName(agent),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(10000)),
						RunAsGroup:     ptr.To(int64(10000)),
						FSGroup:        ptr.To(int64(10000)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:  "broker",
						Image: brokerImage(),
						Args:  brokerArgs(agent),
						// The broker's own write identity, taken from the pod spec by the
						// downward API rather than derived a second time here.
						//
						// This is the value the broker stamps into
						// `ActionRecord.spec.actorServiceAccount`, and the journal admission
						// policy (`kube-agents-agent-scope-journal`) decides whether a status
						// write comes from the owning broker by comparing
						// `system:serviceaccount:<record namespace>:<that field>` against the
						// authenticated user. So the field is not descriptive: it is one half of
						// an equality the API server evaluates, and the other half is the SA the
						// kubelet actually projected a token for.
						//
						// `spec.serviceAccountName` is set eleven lines above from
						// `actorServiceAccountName(agent)`. Writing that call here too would put
						// the two halves of the equality in two places that agree only as long as
						// nobody edits one -- and the failure mode is silent, because a broker
						// that names an identity it does not hold starts, serves, classifies,
						// journals the record, and is refused only on the status write that makes
						// the record mean anything. Reading it back off the pod makes the two
						// halves the same value by construction.
						Env: []corev1.EnvVar{{
							Name: "KAGE_BROKER_SERVICE_ACCOUNT",
							ValueFrom: &corev1.EnvVarSource{
								FieldRef: &corev1.ObjectFieldSelector{
									FieldPath: "spec.serviceAccountName",
								},
							},
						}},
						Ports: []corev1.ContainerPort{{
							Name:          brokerPortName,
							ContainerPort: broker.Port,
							Protocol:      corev1.ProtocolTCP,
						}},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: ptr.To(false),
							ReadOnlyRootFilesystem:   ptr.To(true),
							RunAsNonRoot:             ptr.To(true),
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
						},
						// A tcpSocket probe, not an httpGet one, and this is forced rather than
						// chosen. The listener is `tls.RequireAndVerifyClientCert` (cmd/broker:
						// tlsConfig), so the kubelet — which has no client certificate — cannot
						// complete the handshake and an httpGet probe would report the broker
						// permanently unhealthy. What this buys is therefore weaker than it looks:
						// it proves the port is bound, not that TLS or the handler is well. The
						// stronger check exists and lives where a client certificate does — the
						// agent pod's `wait-for-broker` init container, which does a real mTLS GET
						// of /healthz.
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{
								TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromString(brokerPortName)},
							},
							InitialDelaySeconds: 3,
							PeriodSeconds:       10,
							TimeoutSeconds:      3,
						},
						LivenessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{
								TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromString(brokerPortName)},
							},
							InitialDelaySeconds: 15,
							PeriodSeconds:       20,
							TimeoutSeconds:      3,
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("100m"),
								corev1.ResourceMemory: resource.MustParse("128Mi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("500m"),
								corev1.ResourceMemory: resource.MustParse("512Mi"),
							},
						},
						VolumeMounts: []corev1.VolumeMount{
							{Name: brokerTLSVolumeName, MountPath: brokerTLSMountPath, ReadOnly: true},
							// The one writable path, and it is ephemeral. Go's TLS stack and the
							// client-go transport both want a temp dir; without this the read-only
							// root turns into a runtime error rather than a build-time one.
							{Name: brokerTmpVolumeName, MountPath: "/tmp"},
						},
					}},
					Volumes: []corev1.Volume{
						{
							Name: brokerTLSVolumeName,
							VolumeSource: corev1.VolumeSource{
								Secret: &corev1.SecretVolumeSource{
									SecretName:  brokerTLSSecretName(agent),
									DefaultMode: ptr.To(int32(0400)),
								},
							},
						},
						{
							Name:         brokerTmpVolumeName,
							VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
						},
					},
				},
			},
		},
	}
}

// agentIdentity is the `<tier>/<scope-leaf>` string the agent pod must hash into every
// `idempotencyKey` it computes (06 §4.1), rendered from the CR into KUBEAGENTS_AGENT_IDENTITY.
//
// The pod cannot derive this for itself. It knows its tier — `AGENT_TIER` has been in the general
// env block since the read-only generation — and it has never known its scope leaf: not in an env
// var, not in the rendered ConfigMap, not in the golden manifests. Half an identity is not one, and
// the missing half is precisely the half that distinguishes two agents of the same tier.
//
// The value is composed from the SAME two expressions brokerArgs passes as `--tier` and `--scope`,
// through the SAME production formatter the broker uses to join them. Not a `Sprintf` here that
// happens to agree with `Identity.AgentIdentity()` today: agreement is the entire property, so it is
// obtained by calling the function rather than by matching its output ([[LSN-036]], [[LSN-041]]).
// The empty-scope arm comes along for free, which matters — a platform agent with no
// `spec.scope.projectId` renders bare `platform` on both sides, and a local `tier + "/" + leaf`
// would render `platform/` and be refused on every write.
//
// A mismatch fails closed and that is exactly what makes it dangerous to leave uncompared. The
// broker recomputes the key over its own identity and refuses a difference, so a drifted value is
// not a security hole — it is a total outage of the write path, reported per request as
// `idempotency-key-mismatch`, a message about a hash. V-BRK-030 is the join that keeps the two
// renderings equal.
func agentIdentity(agent *agentv1alpha1.Agent) string {
	id := broker.Identity{
		Tier:  agentindex.EffectiveTier(agent),
		Scope: scope.Of(agent).Leaf(),
	}
	return id.AgentIdentity()
}

// brokerArgs renders the broker's startup configuration.
//
// Flags, not envelope fields, and not env vars read inside the handler. 03 §4.1 step 1 derives
// (tier, scope) from the AUTHENTICATED caller; a broker that could be told its own tier by the
// thing it is authorizing would be authorizing against a claim. Passing them at launch, from the
// controller that already knows them from the CR, is what makes "the caller cannot influence this"
// structural. cmd/broker/main.go refuses to start if any of them is empty.
func brokerArgs(agent *agentv1alpha1.Agent) []string {
	return []string{
		"--agent-name=" + agent.Name,
		"--tier=" + string(agentindex.EffectiveTier(agent)),
		"--scope=" + scope.Of(agent).Leaf(),
		"--namespace=" + agent.Namespace,
		"--reader-service-account=" + readerServiceAccountName(agent),
		"--tls-cert-file=" + brokerTLSMountPath + "/tls.crt",
		"--tls-key-file=" + brokerTLSMountPath + "/tls.key",
		"--client-ca-file=" + brokerTLSMountPath + "/ca.crt",
	}
}

// readerServiceAccountName is the ONE caller identity this broker accepts (08 §2.3). It is the
// agent pod's own SA, resolved exactly the way buildDeployment resolves it — delegating rather than
// re-deriving, because the two disagreeing means the broker refuses every envelope its own agent
// sends, with a `forbidden` that names an identity the reader has never heard of.
func readerServiceAccountName(agent *agentv1alpha1.Agent) string {
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		return agent.Spec.Security.ServiceAccountName
	}
	return agent.Name
}

// buildWaitForBrokerContainer is the `wait-for-broker` init container of 08 §2.4.
//
// # It runs the BROKER image, not the agent's
//
// The poll is an mTLS GET of /healthz with the agent's own mesh certificate, and the agent image is
// a Python harness with no guarantee of a TLS-capable client on PATH. The broker binary already
// speaks exactly this protocol, already knows the flag names, and ships with no shell — so using it
// here adds one image pull the node has already done for the sibling pod and removes a dependency
// on what happens to be installed in a harness image somebody else maintains.
//
// # It exits 0 whether or not the broker answered, and that is the specification
//
// 08 §2.4: "On timeout it starts anyway, in observe-and-report mode — a broker outage must not
// blind the fleet, and an agent that can only read is exactly as safe as the previous generation's
// agent." A non-zero exit here would put the pod in Init:CrashLoopBackOff and take the agent's READ
// path down too, which is the one outcome the design rules out. The verdict is therefore a file on
// a shared emptyDir, not an exit code.
//
// This is not a fail-open: the agent has no write verb to fall back to (03 §11). "Started without a
// broker" means "cannot write", enforced by RBAC, not by the agent choosing to behave.
func buildWaitForBrokerContainer(agent *agentv1alpha1.Agent) corev1.Container {
	return corev1.Container{
		Name:  "wait-for-broker",
		Image: brokerImage(),
		Args: []string{
			"--wait-for-broker",
			"--broker-endpoint=" + brokerEndpoint(agent),
			"--broker-san=" + brokerSAN(agent),
			fmt.Sprintf("--wait-timeout=%ds", waitForBrokerTimeoutSeconds),
			"--status-file=" + agentBrokerStatusMountPath + "/broker-status",
			"--tls-cert-file=" + brokerTLSMountPath + "/tls.crt",
			"--tls-key-file=" + brokerTLSMountPath + "/tls.key",
			"--client-ca-file=" + brokerTLSMountPath + "/ca.crt",
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			RunAsNonRoot:             ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("10m"),
				corev1.ResourceMemory: resource.MustParse("32Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: agentMeshTLSVolumeName, MountPath: brokerTLSMountPath, ReadOnly: true},
			{Name: agentBrokerStatusVolume, MountPath: agentBrokerStatusMountPath},
		},
	}
}

// brokerImage resolves the broker image for both the broker pod and the init container that polls
// it. One function so an operator-level pin cannot be honoured in one place and defaulted in the
// other, which would have the init container speaking a build whose protocol may not match the
// broker's.
//
// # There is deliberately no `spec.deployment.brokerImage`
//
// The agent image IS CR-settable (`spec.deployment.image`), and the asymmetry is the whole point.
// The agent pod holds no write verb, so naming its image chooses which model and which persona run
// inside a container that can only read. The broker pod holds the actor credential, so naming ITS
// image would be choosing which binary sits behind the write authority — the same authority-choosing
// move that 06 §2.2.1 forbids for the actor ServiceAccount, wearing different clothes. A CR author
// who could point the broker at their own image would inherit the actor identity's full scope
// without ever touching an RBAC object.
//
// So this is operator-level configuration, set on the controller Deployment by whoever installs it,
// and the same for every Agent in the cluster.
func brokerImage() string {
	if v := strings.TrimSpace(os.Getenv(brokerImageEnvVar)); v != "" {
		return v
	}
	return defaultBrokerImage
}

// agentBrokerVolumes are the three volumes the agent pod gains for the broker path: its half of the
// mesh keypair, the projected token the broker's TokenReview validates, and the emptyDir carrying
// the init container's verdict.
func agentBrokerVolumes(agent *agentv1alpha1.Agent) []corev1.Volume {
	return []corev1.Volume{
		{
			Name: agentMeshTLSVolumeName,
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName:  agentMeshTLSSecretName(agent),
					DefaultMode: ptr.To(int32(0400)),
				},
			},
		},
		{
			// A projected serviceAccountToken with an explicit audience, NOT the default
			// API-server token (08 §2.3). The default token is valid against the API server, so a
			// broker that accepted it would accept a token minted for a completely different
			// purpose — and the agent's default token is readable by anything running in the
			// agent's pod. `kubeagents-broker` is an audience the API server will not honour, so
			// this file is useless anywhere except at the broker's door.
			Name: agentBrokerTokenVolumeName,
			VolumeSource: corev1.VolumeSource{
				Projected: &corev1.ProjectedVolumeSource{
					DefaultMode: ptr.To(int32(0400)),
					Sources: []corev1.VolumeProjection{{
						ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
							Audience:          broker.TokenAudience,
							ExpirationSeconds: ptr.To(brokerTokenExpirationSeconds),
							Path:              "token",
						},
					}},
				},
			},
		},
		{
			Name:         agentBrokerStatusVolume,
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		},
	}
}

// agentBrokerVolumeMounts are the matching mounts on the agent container. The mesh certificate is
// NOT among them: the agent process talks to the broker through the harness's broker client, which
// reads the cert from the same path the init container used, so the mount is identical — see the
// call site in buildBaseContainers.
func agentBrokerVolumeMounts() []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{Name: agentMeshTLSVolumeName, MountPath: brokerTLSMountPath, ReadOnly: true},
		{Name: agentBrokerTokenVolumeName, MountPath: agentBrokerTokenMountPath, ReadOnly: true},
		{Name: agentBrokerStatusVolume, MountPath: agentBrokerStatusMountPath, ReadOnly: true},
	}
}

// agentBrokerEnvVars are what the harness needs to reach its broker (08 §2.3).
//
// The endpoint is rendered here and mounted read-only, so the agent cannot be pointed at another
// agent's broker by anything it can reach. Even if it could, the foreign broker would reject it:
// the broker accepts exactly one reader identity, its own agent's, and derives (tier, scope) from
// that identity rather than from anything on the wire.
func agentBrokerEnvVars(agent *agentv1alpha1.Agent) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "KUBEAGENTS_AGENT_IDENTITY", Value: agentIdentity(agent)},
		{Name: "KUBEAGENTS_BROKER_ENDPOINT", Value: brokerEndpoint(agent)},
		{Name: "KUBEAGENTS_BROKER_SAN", Value: brokerSAN(agent)},
		{Name: "KUBEAGENTS_BROKER_TOKEN_FILE", Value: agentBrokerTokenMountPath + "/token"},
		{Name: "KUBEAGENTS_BROKER_TLS_DIR", Value: brokerTLSMountPath},
		{Name: "KUBEAGENTS_BROKER_STATUS_FILE", Value: agentBrokerStatusMountPath + "/broker-status"},
	}
}
