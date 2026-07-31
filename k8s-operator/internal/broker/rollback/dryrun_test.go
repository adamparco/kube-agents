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

package rollback_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// The V-REV-003 L1 suite for the production undo.DryRunner.
//
// The interface assertion is the cheapest half of the wiring claim and it belongs here, in the
// consumer's spelling, so that a signature change in `undo` is a compile error rather than a
// broker that starts and validates nothing.
var _ undo.DryRunner = (*rollback.PlanDryRunner)(nil)

// dryRunner assembles the validator over a fake writer, with a reader that agrees with any pin.
func dryRunner(w *fakeWriter) *rollback.PlanDryRunner {
	return &rollback.PlanDryRunner{
		Replayer:      &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}},
		AgentIdentity: identity,
	}
}

func dryRun(t *testing.T, d *rollback.PlanDryRunner, step agentv1alpha1.UndoStep) error {
	t.Helper()
	return d.DryRun(context.Background(), step)
}

// stepFor builds the minimal well-formed step for each op the replayer can replay. Keyed by op so
// that TestThePlanValidatorCoversEveryReplayableOp can drive it off undo.ReplayableOps() rather
// than off a second hand-written list.
func stepFor(t *testing.T, op string) agentv1alpha1.UndoStep {
	t.Helper()
	switch op {
	case "delete":
		return agentv1alpha1.UndoStep{Op: op, Target: deployRef(), Preconditions: pin(testUID)}
	case "apply", "create", "scale":
		return agentv1alpha1.UndoStep{
			Op: op, Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
		}
	default:
		t.Fatalf("undo.ReplayableOps() names %q and this test does not know how to build a step for it -- add one, do not skip it", op)
		return agentv1alpha1.UndoStep{}
	}
}

// TestThePlanValidatorCoversEveryReplayableOp is the check dryrun.go's dispatch doc names, and the
// reason it can be named: a fifth replayable op added to `undo` without a dry-run leg would mean
// every plan using it downgrades to `none` and every action using it gates -- a fleet-wide
// regression whose cause is a missing switch case, showing up as "the broker got cautious".
func TestThePlanValidatorCoversEveryReplayableOp(t *testing.T) {
	ops := undo.ReplayableOps()
	if len(ops) == 0 {
		t.Fatal("undo.ReplayableOps() is empty, so this test asserts nothing")
	}
	for _, op := range ops {
		t.Run(op, func(t *testing.T) {
			w := &fakeWriter{}
			if err := dryRun(t, dryRunner(w), stepFor(t, op)); err != nil {
				t.Fatalf("op %q has no dry-run leg, or its leg refused a well-formed step: %v", op, err)
			}
			if len(w.writes) != 1 {
				t.Fatalf("op %q issued %v against the API server, want exactly one call", op, w.kinds())
			}
		})
	}
}

// TestTheValidatorNeverMutatesAnything is the property that makes it safe to run this before the
// action rather than instead of it. Every leg, every call, dry-run flag set.
//
// It is separate from the coverage test above because they fail for different reasons: that one
// fails when a leg is missing, this one when a leg exists and forgot the flag -- and the second is
// the one that writes to a real cluster while a plan is merely being considered.
func TestTheValidatorNeverMutatesAnything(t *testing.T) {
	for _, op := range undo.ReplayableOps() {
		w := &fakeWriter{}
		if err := dryRun(t, dryRunner(w), stepFor(t, op)); err != nil {
			t.Fatalf("op %q: %v", op, err)
		}
		for _, x := range w.writes {
			if !x.dryRun {
				t.Errorf("op %q issued a %s WITHOUT the dry-run flag: validating a plan changed the cluster", op, x.kind)
			}
		}
	}
}

// TestTheDryRunCarriesTheFieldManagerTheReplayWould.
//
// Server-side apply reports a conflict for every field owned by a different manager, and the fields
// an undo restores are frequently the ones this agent set in an earlier action. A validator issuing
// under any other name manufactures conflicts the real replay never hits -- so the failure is
// over-gating, it is silent, and the only place it is visible is the manager string.
func TestTheDryRunCarriesTheFieldManagerTheReplayWould(t *testing.T) {
	want := "kube-agents/" + identity
	for _, op := range []string{"apply", "create", "scale"} {
		w := &fakeWriter{}
		if err := dryRun(t, dryRunner(w), stepFor(t, op)); err != nil {
			t.Fatalf("op %q: %v", op, err)
		}
		for _, x := range w.writes {
			if x.manager != want {
				t.Errorf("op %q dry-ran as %q, want %q", op, x.manager, want)
			}
		}
	}
}

// --- the two errors that are not failures --------------------------------------------------------
//
// An undo plan describes the world AFTER the action and is validated BEFORE it, so two of the four
// steps address an object whose existence is exactly what the action is about to change. Reading
// those as failures would downgrade every create and every delete in the fleet -- validation that
// gates everything is indistinguishable from no undo at all.

// TestTheDeleteThatReversesACreatePassesOnNotFound. The object does not exist yet; that is the
// point. Kubernetes authorizes before it looks the object up, so a NotFound is positive evidence
// that authn, authz and scope admitted the request.
func TestTheDeleteThatReversesACreatePassesOnNotFound(t *testing.T) {
	w := &fakeWriter{errs: map[writeKind]error{
		wroteDelete: apierrors.NewNotFound(schema.GroupResource{Group: "apps", Resource: "deployments"}, "api-gateway"),
	}}
	if err := dryRun(t, dryRunner(w), stepFor(t, "delete")); err != nil {
		t.Fatalf("a NotFound on the delete leg was read as a failure, which downgrades the undo plan of every create: %v", err)
	}
}

// TestTheDeleteThatReversesACreateDoesNotDemandAUIDThatCannotExistYet.
//
// Replayer.replayDelete requires the uid pin, correctly: at replay time the object exists and the
// pin is what stops a delete landing on a stranger who took the name. At PLAN time there is no uid
// -- undo.BindCreatedUID fills it in after execution -- so requiring it here would gate every
// create in the fleet on a precondition the pipeline has not had a chance to write.
func TestTheDeleteThatReversesACreateDoesNotDemandAUIDThatCannotExistYet(t *testing.T) {
	w := &fakeWriter{}
	step := stepFor(t, "delete")
	step.Preconditions = nil
	if err := dryRun(t, dryRunner(w), step); err != nil {
		t.Fatalf("an unpinned delete was refused at plan time, before BindCreatedUID could have run: %v", err)
	}
}

// TestTheCreateThatReversesADeletePassesOnAlreadyExists. The object is still there and will be
// until the action deletes it. A create runs mutating and validating admission before storage, so
// an AlreadyExists additionally clears every webhook on the path -- it is the strongest of the
// three answers this leg can get.
func TestTheCreateThatReversesADeletePassesOnAlreadyExists(t *testing.T) {
	w := &fakeWriter{errs: map[writeKind]error{
		wroteCreate: apierrors.NewAlreadyExists(schema.GroupResource{Group: "apps", Resource: "deployments"}, "api-gateway"),
	}}
	if err := dryRun(t, dryRunner(w), stepFor(t, "create")); err != nil {
		t.Fatalf("an AlreadyExists on the create leg was read as a failure, which downgrades the undo plan of every delete: %v", err)
	}
}

// --- and everything else is ----------------------------------------------------------------------

// TestARefusalFromTheAPIServerIsAFailedValidation is the negative control for the two tests above:
// without it, "passes on NotFound" could be "passes on anything", and the validator would be a
// function that returns nil.
func TestARefusalFromTheAPIServerIsAFailedValidation(t *testing.T) {
	forbidden := apierrors.NewForbidden(
		schema.GroupResource{Group: "apps", Resource: "deployments"}, "api-gateway",
		errors.New("User \"system:serviceaccount:kube-agents:platform-agent-actor\" cannot delete resource"))

	for _, tc := range []struct {
		op   string
		kind writeKind
	}{
		{"delete", wroteDelete},
		{"apply", wroteApply},
		{"create", wroteCreate},
		{"scale", wroteScale},
	} {
		w := &fakeWriter{errs: map[writeKind]error{tc.kind: forbidden}}
		err := dryRun(t, dryRunner(w), stepFor(t, tc.op))
		if err == nil {
			t.Errorf("op %q: a 403 was read as a step that would apply", tc.op)
			continue
		}
		// The API server's own words, not "validation failed" -- the human deciding whether to
		// approve the gated action needs to know it was an RBAC denial rather than a bad body.
		if !strings.Contains(err.Error(), "cannot delete resource") {
			t.Errorf("op %q: the refusal does not carry the server's reason: %v", tc.op, err)
		}
	}
}

// TestARedactedSecretIsRefusedAtPlanTimeRatherThanDuringAnIncident.
//
// This is the payoff of building the validator on the replayer instead of beside it. The refusal
// itself is not new -- TestARedactedSecretIsRefusedRatherThanRestoredAsDigests already proves the
// replayer will not write sixty-four characters of hex over a credential. What is new is WHEN it
// arrives: reusing hydrate moves it from replay time, during an incident, after the action has run,
// to generation time, where it is a downgrade to `none` and the action gates before mutating
// anything.
func TestARedactedSecretIsRefusedAtPlanTimeRatherThanDuringAnIncident(t *testing.T) {
	w := &fakeWriter{}
	body := rawOf(t, map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "db", "namespace": "team-x"},
		"data":       map[string]any{"password": "sha256:" + strings.Repeat("a", 64)},
	})

	err := dryRun(t, dryRunner(w), agentv1alpha1.UndoStep{
		Op: "apply", Target: secretRef(), Object: body, Preconditions: pin(testUID),
	})
	mustContain(t, err, "REFUSING")
	mustContain(t, err, "data[password]")
	if len(w.writes) != 0 {
		t.Fatalf("the validator wrote %v; a body the replayer would refuse must not reach the API server even as a dry run", w.kinds())
	}
}

// TestAScaleWhoseSnapshotHasNoReplicaCountIsRefused. The step says "put it back to what it was" and
// the snapshot does not say what that was. Passing validation here would produce a plan that
// replays into a nil-replicas apply during an incident.
func TestAScaleWhoseSnapshotHasNoReplicaCountIsRefused(t *testing.T) {
	w := &fakeWriter{}
	step := stepFor(t, "scale")
	step.Object = rawOf(t, map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata":   map[string]any{"name": "api-gateway", "namespace": "team-x"},
		"spec":       map[string]any{},
	})
	err := dryRun(t, dryRunner(w), step)
	mustContain(t, err, "spec.replicas")
	if len(w.writes) != 0 {
		t.Fatalf("the validator issued %v for a scale whose target count is unknown", w.kinds())
	}
}

// TestTheScaleDryRunCarriesTheCountTheReplayWould. A dry run of the wrong number answers a question
// nobody asked: quota and validating webhooks are both functions of the replica count.
func TestTheScaleDryRunCarriesTheCountTheReplayWould(t *testing.T) {
	w := &fakeWriter{}
	if err := dryRun(t, dryRunner(w), stepFor(t, "scale")); err != nil {
		t.Fatalf("scale: %v", err)
	}
	if len(w.writes) != 1 || w.writes[0].replicas != 3 {
		t.Fatalf("writes = %+v, want one scale to 3 -- the count in the snapshot", w.writes)
	}
}

// TestAnOpWithNoReplayImplementationIsRefused. `undo.Validate` turns this into a downgrade, so an
// op the replayer cannot perform gates its action instead of executing with a plan that will fail
// the moment anyone tries it.
func TestAnOpWithNoReplayImplementationIsRefused(t *testing.T) {
	w := &fakeWriter{}
	err := dryRun(t, dryRunner(w), agentv1alpha1.UndoStep{Op: "patch", Target: deployRef()})
	mustContain(t, err, "patch")
	if len(w.writes) != 0 {
		t.Fatalf("the validator issued %v for an op it cannot replay", w.kinds())
	}
}

// TestAValidatorWithNoWriterRefusesRatherThanReportingSuccess is the vacuity guard on the whole
// file: a nil Writer must not read as "every step would apply". That is the shape the wiring defect
// this suite exists to close actually had -- a component that answers the question it was never
// connected to.
func TestAValidatorWithNoWriterRefusesRatherThanReportingSuccess(t *testing.T) {
	for _, d := range []*rollback.PlanDryRunner{
		{AgentIdentity: identity},
		{Replayer: &rollback.Replayer{}, AgentIdentity: identity},
	} {
		if err := dryRun(t, d, stepFor(t, "apply")); err == nil {
			t.Errorf("a validator with no writer reported that the step would apply")
		}
	}
}

// TestADryRunUnderAnUnusableIdentityIsRefused. execute.FieldManager owns the identity rules; the
// point here is that the validator asks it BEFORE issuing anything, so a misconfigured broker
// gates rather than writing under a manager string that belongs to nobody.
func TestADryRunUnderAnUnusableIdentityIsRefused(t *testing.T) {
	w := &fakeWriter{}
	d := dryRunner(w)
	d.AgentIdentity = ""
	if err := dryRun(t, d, stepFor(t, "apply")); err == nil {
		t.Fatal("a validator with no identity reported that the step would apply")
	}
	if len(w.writes) != 0 {
		t.Fatalf("the validator issued %v before it had a field manager", w.kinds())
	}
}
