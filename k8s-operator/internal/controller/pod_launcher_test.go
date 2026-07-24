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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	"github.com/go-logr/logr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func testAgentForLaunch() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "launch-agent", Namespace: "launch-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Security: &agentv1alpha1.SecuritySpec{
					ServiceAccountName: "platform-agent",
				},
				Deployment: &agentv1alpha1.DeploymentSpec{
					RuntimeClassName: ptr.To("gvisor"),
				},
			},
		},
	}
}

// TestSelectPodLauncher_DefaultsToNative asserts that with no gate set the controller uses the
// native builder (v1 default).
func TestSelectPodLauncher_DefaultsToNative(t *testing.T) {
	t.Setenv(scionLaunchEnvVar, "")
	l := selectPodLauncher(logr.Discard())
	if l.name() != "native" {
		t.Fatalf("expected native launcher by default, got %q", l.name())
	}
}

// TestSelectPodLauncher_FallsBackWhenScionUnavailable asserts that even when the Scion gate is
// ON, an absent Scion K8s mode falls back to native — this is the load-bearing spike property.
func TestSelectPodLauncher_FallsBackWhenScionUnavailable(t *testing.T) {
	t.Setenv(scionLaunchEnvVar, "1")
	if scionK8sModeAvailable() {
		t.Skip("Scion K8s mode reported available; fallback path not exercised")
	}
	l := selectPodLauncher(logr.Discard())
	if l.name() != "native" {
		t.Fatalf("expected fallback to native when Scion K8s mode absent, got %q", l.name())
	}
}

// TestLaunchSpecFor extracts the per-pod contract from the CR.
func TestLaunchSpecFor(t *testing.T) {
	spec := launchSpecFor(testAgentForLaunch())
	if spec.Name != "launch-agent" || spec.Namespace != "launch-ns" {
		t.Errorf("unexpected name/namespace: %+v", spec)
	}
	if spec.ServiceAccountName != "platform-agent" {
		t.Errorf("expected serviceAccountName platform-agent, got %q", spec.ServiceAccountName)
	}
	if spec.RuntimeClassName != "gvisor" {
		t.Errorf("expected runtimeClassName gvisor, got %q", spec.RuntimeClassName)
	}
	if !spec.RunAsNonRoot || !spec.SeccompRuntimeDflt {
		t.Errorf("expected hardened posture asserted, got %+v", spec)
	}
}

// TestScionLauncherParityWithNative asserts the Scion launcher (fallback path) produces exactly
// the same pod identity/placement/sandbox/posture as the native builder — so wiring the seam
// never silently changes the deployed pod.
func TestScionLauncherParityWithNative(t *testing.T) {
	agent := testAgentForLaunch()

	native := nativePodLauncher{}.BuildDeployment(agent, "h1", "h2", "h3")
	scion := scionPodLauncher{fallback: nativePodLauncher{}}.BuildDeployment(agent, "h1", "h2", "h3")

	nps := native.Spec.Template.Spec
	sps := scion.Spec.Template.Spec

	if nps.ServiceAccountName != sps.ServiceAccountName {
		t.Errorf("serviceAccountName mismatch: native=%q scion=%q", nps.ServiceAccountName, sps.ServiceAccountName)
	}
	if native.Namespace != scion.Namespace {
		t.Errorf("namespace mismatch: native=%q scion=%q", native.Namespace, scion.Namespace)
	}
	if ptr.Deref(nps.RuntimeClassName, "") != ptr.Deref(sps.RuntimeClassName, "") {
		t.Errorf("runtimeClassName mismatch: native=%v scion=%v", nps.RuntimeClassName, sps.RuntimeClassName)
	}
	if !equalPodSecurity(hardenedPodSpec(native), hardenedPodSpec(scion)) {
		t.Errorf("pod securityContext mismatch: native=%+v scion=%+v", hardenedPodSpec(native), hardenedPodSpec(scion))
	}
}

func equalPodSecurity(a, b *corev1.PodSecurityContext) bool {
	if a == nil || b == nil {
		return a == b
	}
	if ptr.Deref(a.RunAsNonRoot, false) != ptr.Deref(b.RunAsNonRoot, false) {
		return false
	}
	aSecc := a.SeccompProfile != nil && a.SeccompProfile.Type == corev1.SeccompProfileTypeRuntimeDefault
	bSecc := b.SeccompProfile != nil && b.SeccompProfile.Type == corev1.SeccompProfileTypeRuntimeDefault
	return aSecc == bSecc
}
