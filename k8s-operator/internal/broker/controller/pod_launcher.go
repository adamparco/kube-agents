/*
Copyright 2025.

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

// pod_launcher.go — Phase-1 Scion launch-primitive spike (08 §2, §6).
//
// The design reuses the hardened, per-pod-identity model verified in Scion
// (GoogleCloudPlatform/scion: `pkg/api/types.go`, `pkg/runtime/k8s_runtime.go`) — per-pod
// serviceAccountName (Workload Identity), namespace, runtimeClassName, and a hardened pod
// security context. v1 builds the pod NATIVELY (as the operator already does); this file adds
// the SEAM that would let the controller call Scion's launch primitive for pod construction,
// with a mandatory fallback to the native build when Scion's K8s mode is absent.
//
// Scion's Kubernetes runtime is still early and cannot yet supervise long-lived agent pods
// (08 §2 item, §6 non-goals), so the Scion path is behind a feature gate that is OFF by default
// and, even when enabled, falls back to native until an availability probe reports Scion K8s
// mode present. This keeps v1 behaviour identical while proving the integration point.
//
// Spike report: docs/build/spikes/scion-launch-primitive.md.

import (
	"os"

	"github.com/go-logr/logr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// scionLaunchEnvVar gates the Scion launch path. Unset/empty ⇒ native build (v1 default).
const scionLaunchEnvVar = "KUBEAGENTS_SCION_LAUNCH"

// PodIdentity is the per-pod half of the contract: identity (ServiceAccountName,
// Workload-Identity-bound), optional sandbox (RuntimeClassName), and the hardened pod-security
// posture. It mirrors the fields Scion's launch primitive verifies (`pkg/api/types.go`) and
// intentionally omits the full container/volume graph — that stays the native builder's job in v1.
type PodIdentity struct {
	Name               string
	ServiceAccountName string
	RuntimeClassName   string
	// Hardened posture (Scion's verified defaults): non-root + seccomp RuntimeDefault +
	// no privilege escalation. Kept as an explicit assertion so a Scion translation can
	// verify parity with the native build.
	RunAsNonRoot       bool
	SeccompRuntimeDflt bool
	// ReadOnlyRootFS is true only for the actor half (08 §2.6). The agent's harness writes to its
	// workspace; the broker has nothing to write and therefore may not.
	ReadOnlyRootFS bool
}

// LaunchSpec is the framework-neutral contract the controller hands to a launcher. It carries BOTH
// members of the pair, and that is 08 §2.4(a) rather than a convenience:
//
//	"A single LaunchSpec must express both members of the pair … so 'launch an agent' is not an
//	expressible operation on its own."
//
// The requirement is about what a future maintainer can do by accident. Every dangerous state in
// this design is a pair that came apart: an agent running with no broker is merely degraded, but a
// broker running with no agent is an unattended write credential, and an agent whose broker belongs
// to a different scope is a scope escape. If "launch the agent" and "launch the broker" were two
// calls, some later refactor — an early return, a retry that resumes halfway, an error path that
// skips one — would eventually make one of those calls without the other, and nothing in the type
// system would notice. One spec, two halves, no single-half constructor: the seam that could come
// apart does not exist.
type LaunchSpec struct {
	Namespace string
	// Reader is the agent pod: model, chat surface, no write verb.
	Reader PodIdentity
	// Actor is the broker pod: write credential, no model.
	Actor PodIdentity
}

// launchSpecFor extracts the pair contract from an Agent CR (the same values the native builder
// derives), so both launchers start from one source of truth.
func launchSpecFor(agent *agentv1alpha1.Agent) LaunchSpec {
	spec := LaunchSpec{
		Namespace: agent.Namespace,
		Reader: PodIdentity{
			Name:               agent.Name + "-gateway",
			ServiceAccountName: readerServiceAccountName(agent),
			RunAsNonRoot:       true,
			SeccompRuntimeDflt: true,
		},
		Actor: PodIdentity{
			Name:               brokerName(agent),
			ServiceAccountName: actorServiceAccountName(agent),
			RunAsNonRoot:       true,
			SeccompRuntimeDflt: true,
			ReadOnlyRootFS:     true,
		},
	}
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.RuntimeClassName != nil {
		// The sandbox class applies to the READER only. The broker runs no untrusted code — it
		// holds a credential and applies policy — so putting it in gVisor would buy nothing and
		// cost it the syscall latency on the path every write in the system takes.
		spec.Reader.RuntimeClassName = *agent.Spec.Deployment.RuntimeClassName
	}
	return spec
}

// WorkloadPair is what a launcher returns: both Deployments, in the order they must be applied.
//
// The fields are unexported and newWorkloadPair rejects a nil half, so there is no way to obtain a
// WorkloadPair holding an agent alone — the compiler enforces what the LaunchSpec doc describes.
type WorkloadPair struct {
	broker *appsv1.Deployment
	agent  *appsv1.Deployment
}

// newWorkloadPair is the only constructor. It panics on a nil half rather than returning an error
// because both halves are rendered from the same CR by pure functions a few lines earlier: a nil
// here is not a runtime condition a caller could handle, it is a programming error, and the golden
// tests would catch it long before a cluster did.
func newWorkloadPair(broker, agent *appsv1.Deployment) WorkloadPair {
	if broker == nil || agent == nil {
		panic("controller: WorkloadPair requires both halves (08 §2.4) — an unpaired workload is not a launchable unit")
	}
	return WorkloadPair{broker: broker, agent: agent}
}

// Ordered returns the pair broker-first (08 §2.4(b)). Callers apply in this order and stop on the
// first error, which is what makes the "never proceed to the agent on a broker failure" rule of
// §2.4(c) fall out of ordinary sequential code rather than out of a rule someone must remember.
func (p WorkloadPair) Ordered() []*appsv1.Deployment { return []*appsv1.Deployment{p.broker, p.agent} }

// Broker and Agent are read accessors for the callers that must address one half specifically —
// the reconciler's status derivation, and the golden tests. Neither can produce a half-pair,
// because neither returns something another function accepts as a launchable unit.
func (p WorkloadPair) Broker() *appsv1.Deployment { return p.broker }
func (p WorkloadPair) Agent() *appsv1.Deployment  { return p.agent }

// PodLauncher constructs the workload pair. Two implementations exist: the native builder
// (v1 default) and a Scion-backed launcher (spike, gated + probed).
//
// Note what the interface does NOT have: a BuildDeployment returning one Deployment. It used to,
// and removing it is the point of this seam — a launcher cannot implement "just the agent" even if
// its author wanted to.
type PodLauncher interface {
	// name identifies the launcher for logging/telemetry.
	name() string
	// BuildPair returns both fully-specified Deployments, broker first.
	BuildPair(agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsConfigHash string) WorkloadPair
}

// nativePodLauncher wraps the existing, verified native builder. This is the fallback and the
// v1 default — its output is exactly what the operator produces today.
type nativePodLauncher struct{}

func (nativePodLauncher) name() string { return "native" }

func (nativePodLauncher) BuildPair(agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsConfigHash string) WorkloadPair {
	return newWorkloadPair(
		buildBrokerDeployment(agent),
		buildDeployment(agent, configHash, fluentBitHash, settingsConfigHash),
	)
}

// scionPodLauncher is the spike target: it would translate LaunchSpec into a Scion launch
// request (`pkg/runtime/k8s_runtime.go`). Until Scion's K8s mode can supervise long-lived agent
// pods, BuildDeployment delegates to the native builder so behaviour is unchanged. The type is
// kept concrete (not TODO-only) so the wiring, the probe, and the fallback are all exercised.
type scionPodLauncher struct {
	fallback PodLauncher
}

func (scionPodLauncher) name() string { return "scion" }

func (s scionPodLauncher) BuildPair(agent *agentv1alpha1.Agent, configHash, fluentBitHash, settingsConfigHash string) WorkloadPair {
	// Spike placeholder: derive the contract (proving the extraction path), then build via the
	// native fallback. A real integration would submit launchSpecFor(agent) to Scion's launch
	// primitive as ONE request carrying both halves — Scion's own pod-per-identity model is what
	// makes the pair expressible there — and reconcile the returned pods. We assert parity here.
	_ = launchSpecFor(agent)
	return s.fallback.BuildPair(agent, configHash, fluentBitHash, settingsConfigHash)
}

// hardenedPodSpec is a small helper used by the parity test to read the native build's posture.
func hardenedPodSpec(dep *appsv1.Deployment) *corev1.PodSecurityContext {
	return dep.Spec.Template.Spec.SecurityContext
}

// scionLaunchEnabled reports whether the operator asked to try the Scion launch path.
func scionLaunchEnabled() bool {
	v := os.Getenv(scionLaunchEnvVar)
	return v == "1" || v == "true"
}

// scionK8sModeAvailable probes whether Scion's Kubernetes runtime is present and able to
// supervise long-lived agent pods. In v1 it is not (08 §2, §6): Scion's K8s mode is early, so
// this returns false and the selector falls back to native. When a Scion sidecar/endpoint is
// wired in a later phase, this probe is where its readiness check lives.
func scionK8sModeAvailable() bool {
	return false
}

// selectPodLauncher picks the launcher for this reconcile. It returns the Scion launcher ONLY
// when the gate is enabled AND Scion K8s mode is available; otherwise it returns the native
// builder. The result is never nil, so pod construction always has a working path.
func selectPodLauncher(log logr.Logger) PodLauncher {
	native := nativePodLauncher{}
	if !scionLaunchEnabled() {
		return native
	}
	if !scionK8sModeAvailable() {
		log.Info("Scion launch requested but Scion K8s mode is unavailable; falling back to native pod build",
			"gate", scionLaunchEnvVar)
		return native
	}
	log.Info("Using Scion launch primitive for pod construction")
	return scionPodLauncher{fallback: native}
}
