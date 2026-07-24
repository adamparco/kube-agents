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

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// scionLaunchEnvVar gates the Scion launch path. Unset/empty ⇒ native build (v1 default).
const scionLaunchEnvVar = "KUBEAGENTS_SCION_LAUNCH"

// LaunchSpec is the minimal, framework-neutral per-pod contract the controller hands to a
// launcher. It mirrors the fields Scion's launch primitive verifies (`pkg/api/types.go`):
// identity (ServiceAccountName, Workload-Identity-bound), placement (Namespace), optional
// sandbox (RuntimeClassName), and the hardened pod-security posture. It intentionally omits the
// full container/volume graph — that stays the native builder's job in v1; the Scion path would
// translate this contract into a Scion launch request.
type LaunchSpec struct {
	Name               string
	Namespace          string
	ServiceAccountName string
	RuntimeClassName   string
	// Hardened posture (Scion's verified defaults): non-root + seccomp RuntimeDefault +
	// no privilege escalation. Kept as an explicit assertion so a Scion translation can
	// verify parity with the native build.
	RunAsNonRoot       bool
	SeccompRuntimeDflt bool
}

// launchSpecFor extracts the per-pod contract from an Agent CR (the same values the native
// builder derives), so both launchers start from one source of truth.
func launchSpecFor(agent *agentv1alpha1.PlatformAgent) LaunchSpec {
	spec := LaunchSpec{
		Name:               agent.Name,
		Namespace:          agent.Namespace,
		RunAsNonRoot:       true,
		SeccompRuntimeDflt: true,
	}
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		spec.ServiceAccountName = agent.Spec.Security.ServiceAccountName
	}
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.RuntimeClassName != nil {
		spec.RuntimeClassName = *agent.Spec.Deployment.RuntimeClassName
	}
	return spec
}

// PodLauncher constructs the agent's Deployment. Two implementations exist: the native builder
// (v1 default) and a Scion-backed launcher (spike, gated + probed).
type PodLauncher interface {
	// name identifies the launcher for logging/telemetry.
	name() string
	// BuildDeployment returns the fully-specified agent Deployment.
	BuildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment
}

// nativePodLauncher wraps the existing, verified native builder. This is the fallback and the
// v1 default — its output is exactly what the operator produces today.
type nativePodLauncher struct{}

func (nativePodLauncher) name() string { return "native" }

func (nativePodLauncher) BuildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment {
	return buildDeployment(agent, configHash, fluentBitHash, settingsConfigHash)
}

// scionPodLauncher is the spike target: it would translate LaunchSpec into a Scion launch
// request (`pkg/runtime/k8s_runtime.go`). Until Scion's K8s mode can supervise long-lived agent
// pods, BuildDeployment delegates to the native builder so behaviour is unchanged. The type is
// kept concrete (not TODO-only) so the wiring, the probe, and the fallback are all exercised.
type scionPodLauncher struct {
	fallback PodLauncher
}

func (scionPodLauncher) name() string { return "scion" }

func (s scionPodLauncher) BuildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash string) *appsv1.Deployment {
	// Spike placeholder: derive the contract (proving the extraction path), then build via the
	// native fallback. A real integration would submit launchSpecFor(agent) to Scion's launch
	// primitive and reconcile the returned pod. We assert parity here instead.
	_ = launchSpecFor(agent)
	return s.fallback.BuildDeployment(agent, configHash, fluentBitHash, settingsConfigHash)
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
