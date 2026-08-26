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
	"reflect"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// V-RUN-012 / V-RUN-007: `pause` is structurally not a scale-to-zero.
//
// 08 §2.4 and 06 §4.4 both say the pod keeps running while an agent is paused: it keeps its work
// queue, keeps observing, and keeps being able to say why it is refusing. Only the WRITE path
// closes, and it closes in the broker rather than in the pod's replica count.
//
// This is not a hypothetical requirement in this repository. `resolveDeploymentReplicasAndStrategy`
// already contains a real scale-to-zero branch -- `spec.deployment.scaleToZero`, an unrelated
// idling feature -- so "make pause set replicas to 0" is a one-line change three lines away from
// code that already does exactly that, and it would look like a tidy reuse rather than a
// regression. That is the change these two halves exist to stop:
//
//   - THIS test asserts the PROPERTY: a paused Agent renders a byte-identical Deployment.
//   - `dev/tests/pause-is-not-scale-to-zero.py` asserts the SHAPE: the function that decides
//     replicas cannot see `spec.operations` at all, because it is not given it. That half runs on
//     L0 with no Go toolchain, and it is the half that survives a refactor which moves the
//     rendering somewhere this test no longer covers.

// pausableAgent is a fully-specified Agent whose brake state the caller sets. Deliberately not
// minimal: a Deployment rendered from a near-empty spec exercises defaults rather than the
// rendering, and two identical near-empty results prove much less than two identical rich ones.
func pausableAgent(ops *agentv1alpha1.OperationsSpec) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "brake-agent", Namespace: "team-x"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:       agentv1alpha1.TierDeveloperTeam,
			Scope:      &agentv1alpha1.ScopeSpec{ProjectID: "p", ClusterName: "c", Namespace: "team-x"},
			Operations: ops,
			Deployment: &agentv1alpha1.DeploymentSpec{
				Image: "gcr.io/p/developer-team-agent",
				Tag:   ptr.To("v1.0.0"),
			},
			Harness: &agentv1alpha1.HarnessSpec{},
		},
	}
}

// TestPauseDoesNotChangeTheRenderedDeployment is the V-RUN-012 property at L1.
//
// Deep equality over the whole Deployment rather than a replica-count assertion, because the
// requirement is not merely "replicas stays 1". 06 §4.4 says the POD keeps running -- same pod, same
// UID, same start time -- and any difference in the rendered Deployment produces a new ReplicaSet
// and therefore a new pod. A change to the pod template is a rolling restart wearing a different
// hat, and it fails V-RUN-012's "the pod UID and its start time are unchanged" clause just as
// surely as replicas: 0 would.
func TestPauseDoesNotChangeTheRenderedDeployment(t *testing.T) {
	running := buildDeployment(pausableAgent(nil), "cfg", "fb", "set")

	brakes := map[string]*agentv1alpha1.OperationsSpec{
		"paused": {
			Paused:      ptr.To(true),
			PauseReason: "INC-4471 — payments degraded",
		},
		"paused with every operations field set": {
			Paused:            ptr.To(true),
			PauseReason:       "INC-4471",
			DryRunOnly:        ptr.To(true),
			NotifyOn:          agentv1alpha1.NotifyGated,
			ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: "team-x-approvers"},
			ChangePolicyRefs:  []agentv1alpha1.PolicyRef{{Name: "baseline-conservative"}},
		},
		"dry-run only": {
			DryRunOnly: ptr.To(true),
		},
		"explicitly not paused": {
			Paused: ptr.To(false),
		},
	}

	for name, ops := range brakes {
		t.Run(name, func(t *testing.T) {
			braked := buildDeployment(pausableAgent(ops), "cfg", "fb", "set")

			if got := *braked.Spec.Replicas; got != 1 {
				t.Errorf("replicas is %d, want 1: pause is not scale-to-zero (08 §2.4, V-RUN-012) — "+
					"a paused agent keeps its pod, its queue, and its ability to report why it is refusing", got)
			}
			if !reflect.DeepEqual(running.Spec, braked.Spec) {
				t.Errorf("the rendered Deployment spec differs between a running and a %q agent.\n"+
					"Any difference here rolls the pod, which changes the pod UID and start time that "+
					"V-RUN-012 requires to be unchanged across a pause/resume cycle. The brake belongs "+
					"in the broker's refusal path, not in the workload.\nrunning: %+v\nbraked:  %+v",
					name, running.Spec, braked.Spec)
			}
		})
	}
}

// TestPauseDoesNotChangeTheRenderedBroker extends the same property to the other half of the pair.
//
// Added with the broker in P9-T7b, because "pause = scale the broker to zero" is the more tempting
// of the two wrong implementations, not the less: pausing an agent DOES mean it must not write, and
// the broker is the only thing in the pair that can. Removing it looks like closing the write path
// at its source.
//
// What it actually removes is the explanation. 06 §4.4 wants a paused agent to keep saying why it
// is refusing, and the refusal comes from the broker — so a fleet-wide pause implemented this way
// reports itself to every operator as a broker outage, and `wait-for-broker` puts the pods into
// observe-and-report, which is the same words for a different situation. Pause has to be a brake
// the broker applies while running, not the broker's absence.
func TestPauseDoesNotChangeTheRenderedBroker(t *testing.T) {
	running := buildBrokerDeployment(pausableAgent(nil))

	braked := buildBrokerDeployment(pausableAgent(&agentv1alpha1.OperationsSpec{
		Paused:      ptr.To(true),
		PauseReason: "INC-4471 — payments degraded",
		DryRunOnly:  ptr.To(true),
	}))

	if got := *braked.Spec.Replicas; got != 1 {
		t.Errorf("broker replicas is %d, want 1: a paused agent's broker keeps running so it can "+
			"refuse with a reason (06 §4.4, V-RUN-012)", got)
	}
	if !reflect.DeepEqual(running.Spec, braked.Spec) {
		t.Errorf("the rendered broker Deployment differs between a running and a paused agent.\n"+
			"running: %+v\nbraked:  %+v", running.Spec, braked.Spec)
	}

	// And the brake must not reach the broker through the agent's own scale-to-zero either: an
	// idled agent still has a broker, because `scaleToZero` idles the reader and says nothing
	// about the write path.
	idled := pausableAgent(nil)
	idled.Spec.Deployment.ScaleToZero = ptr.To(true)
	if got := *buildBrokerDeployment(idled).Spec.Replicas; got != 1 {
		t.Errorf("broker replicas is %d under spec.deployment.scaleToZero, want 1", got)
	}
}

// TestScaleToZeroStillWorks is the negative control for the test above.
//
// Without it, `TestPauseDoesNotChangeTheRenderedDeployment` passes just as happily against a
// `buildDeployment` that ignores its argument entirely, or one that hard-codes `replicas: 1` and
// drops the whole `scaleToZero` feature. "The two renders are identical" is only evidence that
// pause is inert if the renderer is known to be capable of producing a difference at all.
//
// `spec.deployment.scaleToZero` is the right control precisely because it is the SAME mechanism
// V-RUN-012 forbids pause from using, reached through the field that is allowed to use it.
func TestScaleToZeroStillWorks(t *testing.T) {
	agent := pausableAgent(nil)
	agent.Spec.Deployment.ScaleToZero = ptr.To(true)

	dep := buildDeployment(agent, "cfg", "fb", "set")
	if got := *dep.Spec.Replicas; got != 0 {
		t.Fatalf("spec.deployment.scaleToZero rendered replicas=%d, want 0. This test is the negative "+
			"control for TestPauseDoesNotChangeTheRenderedDeployment: if the renderer can no longer "+
			"produce a replica difference, that test proves nothing", got)
	}
}

// TestBrakeIsReadableWithoutTheOperationsBlock pins the defaults the brake reads.
//
// Every one of them defaults to the PERMISSIVE value, which is the correct direction for a spec
// field -- an Agent with no `operations` block is a normal working agent, not a bricked one -- and
// is also the direction that hides a nil-dereference guard someone forgot. `Brake()` exists so
// there is one place that applies all three defaults together, and this pins that it does.
func TestBrakeIsReadableWithoutTheOperationsBlock(t *testing.T) {
	cases := []struct {
		name       string
		ops        *agentv1alpha1.OperationsSpec
		wantPaused bool
		wantDryRun bool
		wantReason string
	}{
		{"nil operations", nil, false, false, ""},
		{"empty operations", &agentv1alpha1.OperationsSpec{}, false, false, ""},
		{"paused with a reason", &agentv1alpha1.OperationsSpec{Paused: ptr.To(true), PauseReason: "INC-1"}, true, false, "INC-1"},
		{"dry-run only", &agentv1alpha1.OperationsSpec{DryRunOnly: ptr.To(true)}, false, true, ""},
		{"both", &agentv1alpha1.OperationsSpec{Paused: ptr.To(true), DryRunOnly: ptr.To(true)}, true, true, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			paused, dryRun, reason := tc.ops.Brake()
			if paused != tc.wantPaused || dryRun != tc.wantDryRun || reason != tc.wantReason {
				t.Errorf("Brake() = (%v, %v, %q), want (%v, %v, %q)",
					paused, dryRun, reason, tc.wantPaused, tc.wantDryRun, tc.wantReason)
			}
		})
	}

	if got := (*agentv1alpha1.OperationsSpec)(nil).EffectiveNotifyOn(); got != agentv1alpha1.NotifyElevated {
		t.Errorf("nil operations EffectiveNotifyOn() = %q, want %q", got, agentv1alpha1.NotifyElevated)
	}
}
