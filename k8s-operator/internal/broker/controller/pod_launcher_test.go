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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	"github.com/go-logr/logr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func testAgentForLaunch() *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "launch-agent", Namespace: "launch-ns"},
		Spec: agentv1alpha1.AgentSpec{
			Security: &agentv1alpha1.SecuritySpec{
				ServiceAccountName: "platform-agent",
			},
			Deployment: &agentv1alpha1.DeploymentSpec{
				RuntimeClassName: ptr.To("gvisor"),
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

// TestLaunchSpecFor extracts the PAIR contract from the CR (08 §2.4): both halves, with distinct
// identities, and the sandbox class applied only to the reader.
func TestLaunchSpecFor(t *testing.T) {
	spec := launchSpecFor(testAgentForLaunch())
	if spec.Namespace != "launch-ns" {
		t.Errorf("unexpected namespace: %+v", spec)
	}
	if spec.Reader.Name != "launch-agent-gateway" {
		t.Errorf("expected reader launch-agent-gateway, got %q", spec.Reader.Name)
	}
	if spec.Actor.Name != "launch-agent-broker" {
		t.Errorf("expected actor launch-agent-broker, got %q", spec.Actor.Name)
	}
	if spec.Reader.ServiceAccountName != "platform-agent" {
		t.Errorf("expected reader SA platform-agent, got %q", spec.Reader.ServiceAccountName)
	}
	// The credential split of 08 §2.2 is exactly this inequality. If the two halves ever resolve
	// to one ServiceAccount, the pod separation is decoration.
	if spec.Actor.ServiceAccountName == spec.Reader.ServiceAccountName {
		t.Errorf("reader and actor must not share a ServiceAccount, both are %q", spec.Actor.ServiceAccountName)
	}
	if spec.Reader.RuntimeClassName != "gvisor" {
		t.Errorf("expected reader runtimeClassName gvisor, got %q", spec.Reader.RuntimeClassName)
	}
	if spec.Actor.RuntimeClassName != "" {
		t.Errorf("the sandbox class applies to the reader only, got actor %q", spec.Actor.RuntimeClassName)
	}
	for _, half := range []struct {
		what string
		id   PodIdentity
	}{{"reader", spec.Reader}, {"actor", spec.Actor}} {
		if !half.id.RunAsNonRoot || !half.id.SeccompRuntimeDflt {
			t.Errorf("expected hardened posture asserted for %s, got %+v", half.what, half.id)
		}
	}
	if !spec.Actor.ReadOnlyRootFS {
		t.Error("08 §2.6 requires a read-only root filesystem on the broker")
	}
}

// TestWorkloadPairRejectsAHalf is the compile-time-adjacent half of 08 §2.4(a): "launch an agent"
// must not be an expressible operation. The constructor is the only way to obtain a WorkloadPair
// and it refuses a nil half, so the unpaired state has no representation for a caller to reach.
func TestWorkloadPairRejectsAHalf(t *testing.T) {
	agent := testAgentForLaunch()
	full := nativePodLauncher{}.BuildPair(agent, "h1", "h2", "h3")

	for _, tc := range []struct {
		name           string
		broker, worker *appsv1.Deployment
	}{
		{"agent alone", nil, full.Agent()},
		{"broker alone", full.Broker(), nil},
		{"neither", nil, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			defer func() {
				if recover() == nil {
					t.Fatal("expected newWorkloadPair to reject a half-pair")
				}
			}()
			_ = newWorkloadPair(tc.broker, tc.worker)
		})
	}
}

// TestBuildPairOrdersBrokerFirst pins 08 §2.4(b). The reconciler applies Ordered() in sequence and
// returns on the first error, so this order is what makes "never proceed to the agent on a broker
// failure" (§2.4(c)) a property of the data rather than of a rule someone must remember.
func TestBuildPairOrdersBrokerFirst(t *testing.T) {
	pair := nativePodLauncher{}.BuildPair(testAgentForLaunch(), "h1", "h2", "h3")
	ordered := pair.Ordered()
	if len(ordered) != 2 {
		t.Fatalf("expected exactly two workloads, got %d", len(ordered))
	}
	if ordered[0].Name != "launch-agent-broker" {
		t.Errorf("broker must be applied first, got %q", ordered[0].Name)
	}
	if ordered[1].Name != "launch-agent-gateway" {
		t.Errorf("agent must be applied second, got %q", ordered[1].Name)
	}
}

// TestScionLauncherParityWithNative asserts the Scion launcher (fallback path) produces exactly
// the same pod identity/placement/sandbox/posture as the native builder — so wiring the seam
// never silently changes the deployed pod.
func TestScionLauncherParityWithNative(t *testing.T) {
	agent := testAgentForLaunch()

	native := nativePodLauncher{}.BuildPair(agent, "h1", "h2", "h3").Agent()
	scion := scionPodLauncher{fallback: nativePodLauncher{}}.BuildPair(agent, "h1", "h2", "h3").Agent()

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
