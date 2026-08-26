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

package pipeline

import (
	"errors"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// V-BRK-022 and the verb-set join it rests on. [[LSN-040]] is the lesson both close.
//
// The lesson was that classify and execute each held a correct, tested reading of `WholeObject` and
// meant different things by it, so `apply` could not traverse the pipeline at all -- for the entire
// period in which a table covering `create`, `delete` and JSON-patch `patch` printed green. The
// mechanization therefore cannot be another table of verbs somebody wrote down. It has to DISCOVER
// the verb set from the enum the envelope validator itself uses, so that the day a sixth verb is
// admitted, this file is red until someone drives it through.

func liveDeployment(replicas, available int64) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata": map[string]any{
			"name":            "checkout",
			"namespace":       testTenantNS,
			"uid":             "99999999-8888-7777-6666-555555555555",
			"resourceVersion": "2048",
			"generation":      int64(4),
		},
		"spec":   map[string]any{"replicas": replicas},
		"status": map[string]any{"observedGeneration": int64(4), "availableReplicas": available},
	}}
}

func deploymentTarget() *broker.Target {
	return &broker.Target{
		Group: "apps", Version: "v1", Kind: "Deployment",
		Namespace: testTenantNS, Name: "checkout",
	}
}

// verbCase is one verb driven all the way through. Every field describes the WORLD the verb acts
// on; none of them describes the expected outcome, because the outcome asserted is the same for
// every verb and is the point of the check: the action executed and was verified.
type verbCase struct {
	env    *broker.Envelope
	tweaks []func(*rig)
}

// verbCases is keyed by the envelope op it exercises. The keys are checked against
// broker.ValidOps() rather than trusted, in both directions.
func verbCases() map[string]verbCase {
	replicas := int32(5)
	patched := liveConfigMap()
	patched.Object["data"] = map[string]any{"log-level": "debug"}

	scaled := liveDeployment(5, 5)

	return map[string]verbCase{
		"create": {
			env: createEnvelope(),
			// The reference case: nothing there yet, so the rig's default absent reader is right.
			tweaks: []func(*rig){func(r *rig) { r.prober = &fakeProber{obj: liveConfigMap()} }},
		},
		"apply": {
			env: applyEnvelope("debug"),
			tweaks: []func(*rig){func(r *rig) {
				r.reader = &fakeReader{obj: liveConfigMap()}
				r.prober = &fakeProber{obj: patched}
			}},
		},
		"patch": {
			// A MERGE patch, not a JSON patch. The JSON-patch media type is the one verb-shape that
			// arrives already carrying its own operation list; every other field-level verb has its
			// path set computed by fillTouchedPaths, and the merge patch is the one that exercises
			// that computation through the whole pipeline rather than at the seam.
			env: mergePatchEnvelope("debug"),
			tweaks: []func(*rig){func(r *rig) {
				r.reader = &fakeReader{obj: liveConfigMap()}
				r.applier = &fakeApplier{result: patched}
				r.prober = &fakeProber{obj: patched}
			}},
		},
		"scale": {
			env: scaleEnvelope(replicas),
			tweaks: []func(*rig){func(r *rig) {
				r.reader = &fakeReader{obj: liveDeployment(3, 3)}
				r.applier = &fakeApplier{result: scaled}
				r.prober = &fakeProber{obj: scaled, restarts: 0}
			}},
		},
		"delete": {
			env: deleteEnvelope(),
			tweaks: []func(*rig){func(r *rig) {
				r.reader = &fakeReader{obj: liveDeployment(3, 3)}
				// Gone afterwards, which is what a delete that worked looks like to a prober. If
				// the object were still there the verification would have to fail, and a fixture
				// that left it there would be asserting the wrong thing about the right verb.
				r.prober = &fakeProber{absent: true}
			}},
		},
	}
}

func mergePatchEnvelope(v string) *broker.Envelope {
	env := createEnvelope()
	env.Intent = "raise the log level"
	env.Operations[0].Op = "patch"
	env.Operations[0].DesiredState = nil
	env.Operations[0].Patch = &broker.Patch{
		Type: "application/merge-patch+json",
		Body: map[string]any{"data": map[string]any{"log-level": v}},
	}
	return env
}

func scaleEnvelope(replicas int32) *broker.Envelope {
	env := createEnvelope()
	env.Intent = "scale the checkout deployment"
	env.Operations[0].Op = "scale"
	env.Operations[0].Target = deploymentTarget()
	env.Operations[0].DesiredState = nil
	env.Operations[0].Scale = &broker.ScaleSpec{Replicas: &replicas}
	return env
}

func deleteEnvelope() *broker.Envelope {
	env := createEnvelope()
	env.Intent = "remove the retired checkout deployment"
	env.Operations[0].Op = "delete"
	// A Deployment, not the ConfigMap every other case uses. `delete ConfigMap` is code-floored to
	// `gated` -- ConfigMap is in classify's statefulKinds -- so an envelope built on it parks for
	// approval and never executes. That is the floor working, and asserting it here would leave
	// `delete` itself the one verb in the enum this check never drives.
	env.Operations[0].Target = deploymentTarget()
	env.Operations[0].DesiredState = nil
	return env
}

// TestEveryEnvelopeVerbExecutesEndToEnd is V-BRK-022.
//
// "End to end" is load-bearing and means the assembled pipeline, not a seam: the classifier is fed
// by the same conversion the broker uses, the executor is the real execute.Executor, and the
// verifier is the real verify.Driver. LSN-040's defect lived in none of those packages and in all
// of the wiring between them, and every package's own tests were green throughout.
func TestEveryEnvelopeVerbExecutesEndToEnd(t *testing.T) {
	cases := verbCases()

	// Discovered, not restated. A verb added to broker.validOps with no case here fails, which is
	// the anti-headcount property (LSN-036) -- and a case here for a verb the envelope no longer
	// admits fails too, because a fixture exercising a verb nothing can submit is coverage that
	// only exists in the coverage report.
	enum := broker.ValidOps()
	if len(enum) == 0 {
		t.Fatal("broker.ValidOps() is empty, so the loop below asserts nothing")
	}
	inEnum := map[string]bool{}
	for _, verb := range enum {
		inEnum[verb] = true
		if _, ok := cases[verb]; !ok {
			t.Errorf("the envelope admits op %q and no case drives it through the pipeline; "+
				"V-BRK-022 is EVERY verb in the enum, and the verb with no case is the one that does not work", verb)
		}
	}
	for verb := range cases {
		if !inEnum[verb] {
			t.Errorf("there is a case for op %q, which broker.ValidOps() does not admit; "+
				"an envelope carrying it is refused at validation, so this case proves nothing", verb)
		}
	}

	for _, verb := range enum {
		tc, ok := cases[verb]
		if !ok {
			continue // already reported above
		}
		t.Run(verb, func(t *testing.T) {
			r := newRig(t, tc.tweaks...)
			tr, res, err := r.submit(tc.env)
			if err != nil {
				t.Fatalf("op %q was refused: %v\ntrace: %s", verb, err, tr)
			}
			if res.Decision != "accepted" {
				t.Fatalf("op %q: decision = %q, want accepted\ntrace: %s", verb, res.Decision, tr)
			}
			// The trace reaching step 11 is not on its own evidence the cluster changed: a verb
			// that classified, gated nothing and quietly applied zero operations would produce an
			// identical trace. The mutation count is what distinguishes them.
			if r.applier.mutations != 1 {
				t.Errorf("op %q issued %d real mutations, want 1", verb, r.applier.mutations)
			}
			if got := tr.Reached(); got != broker.LastStep {
				t.Errorf("op %q reached %s, want %s\ntrace: %s", verb, got, broker.LastStep, tr)
			}
			if res.Phase != string(agentv1alpha1.PhaseVerified) {
				t.Errorf("op %q: phase = %q, want %q\ntrace: %s", verb, res.Phase, agentv1alpha1.PhaseVerified, tr)
			}
			// And the durable half. An action that executed and journalled no undo plan is one a
			// human cannot reverse, which invariant 3 forbids for every verb -- including the ones
			// that only started executing today.
			if len(r.records.stored) != 1 {
				t.Fatalf("op %q stored %d records, want 1", verb, len(r.records.stored))
			}
			ar := r.records.stored[0]
			if ar.Spec.Undo == nil || ar.Spec.Undo.Strategy == agentv1alpha1.UndoNone {
				t.Errorf("op %q journalled no usable undo plan: %+v", verb, ar.Spec.Undo)
			}
		})
	}
}

// TestClassifyKnownVerbsAgreeWithTheEnvelopeEnum is the join [[LSN-040]] asked for and the control
// classify/rule.go claimed to have.
//
// classify keeps its own copy of the verb set because it is imported BY the broker and cannot
// import back. That is a sound reason for a second copy and no reason at all for an uncompared one:
// a verb in the envelope that classify does not know is refused at Input.Validate as "not an
// envelope op" -- the broker rejecting its own vocabulary -- and a verb classify knows that no
// envelope can carry is a ChangePolicy rule that matches nothing while reading as a control.
func TestClassifyKnownVerbsAgreeWithTheEnvelopeEnum(t *testing.T) {
	envelope := map[string]bool{}
	for _, v := range broker.ValidOps() {
		envelope[v] = true
	}
	known := map[string]bool{}
	for _, v := range classify.KnownVerbs() {
		known[v] = true
	}
	if len(envelope) == 0 || len(known) == 0 {
		t.Fatal("one of the two sets is empty, so agreeing with the other proves nothing")
	}

	for v := range envelope {
		if !known[v] {
			t.Errorf("the envelope admits op %q and classify does not know it: every operation "+
				"carrying it is refused at classify.Input.Validate, whatever the rules say", v)
		}
	}
	for v := range known {
		if envelope[v] {
			continue
		}
		why, declared := classify.VerbsNotCarriedByAnEnvelopeOp[v]
		if !declared {
			t.Errorf("classify matches rules on verb %q, which no envelope op can carry, and "+
				"nothing says why; a rule naming it gates nothing", v)
			continue
		}
		if len(strings.TrimSpace(why)) < 40 {
			t.Errorf("verb %q is declared as not-an-envelope-op with a reason too short to be one: %q", v, why)
		}
	}
	// The other direction of the declaration, which is the half that goes stale. An entry that
	// becomes a real envelope op keeps excusing a divergence that no longer exists, and the next
	// reader takes the excuse at face value.
	for v := range classify.VerbsNotCarriedByAnEnvelopeOp {
		if envelope[v] {
			t.Errorf("%q is declared as a verb no envelope can carry, and broker.ValidOps() carries it", v)
		}
		if !known[v] {
			t.Errorf("%q is declared as a classify verb outside the envelope enum, and classify does not know it either", v)
		}
	}
}

// TestNoCloudTargetReachesTheClassifier is what makes the `cloud` entry above a property rather
// than a claim.
//
// `cloud` is in classify's verb set and in the code floor's writeVerbs, and no envelope can carry
// it: a Config Connector action arrives as an ordinary verb against a *.cnrm.cloud.google.com kind.
// The divergence is only safe while this broker refuses cloudTargets outright. If that refusal is
// ever removed without the verb being emitted, a rule reading `verbs: [cloud]` will still match
// nothing -- and it will be gating a real cloud write by then.
func TestNoCloudTargetReachesTheClassifier(t *testing.T) {
	env := createEnvelope()
	env.Operations[0].Target = nil
	env.Operations[0].CloudTarget = &broker.CloudTarget{
		Service: "compute", Resource: "projects/p/zones/z/instances/i",
	}

	r := newRig(t)
	tr, res, err := r.submit(env)
	if err == nil {
		t.Fatalf("a cloudTarget was accepted (%+v)\ntrace: %s", res, tr)
	}
	var ref *broker.Refusal
	if !errors.As(err, &ref) {
		t.Fatalf("error is %T, want a *broker.Refusal: %v", err, err)
	}
	if ref.Reason != "cloud-target-unavailable" {
		t.Errorf("reason = %q, want cloud-target-unavailable", ref.Reason)
	}
	if r.applier.mutations != 0 || r.applier.dryRuns != 0 {
		t.Errorf("a refused cloud action reached the applier")
	}
}

// TestTheVerbAssertionsCanFail is this file's negative control.
//
// Every assertion above is of the form "the pipeline did the right thing". A rig misconfigured so
// that nothing runs would satisfy several of them by accident, so the control drives the same
// pipeline with one thing deliberately wrong and requires the same assertions to catch it.
func TestTheVerbAssertionsCanFail(t *testing.T) {
	// A delete whose object is still present afterwards. The verb executes; the outcome is not
	// what the action claimed, and step 10 is the only thing standing between the two.
	r := newRig(t, func(r *rig) {
		r.reader = &fakeReader{obj: liveDeployment(3, 3)}
		r.prober = &fakeProber{obj: liveDeployment(3, 3)}
	})
	_, res, err := r.submit(deleteEnvelope())
	if err == nil && res != nil && res.Phase == string(agentv1alpha1.PhaseVerified) {
		t.Fatal("a delete whose target still exists afterwards was reported Verified, so the phase assertion in TestEveryEnvelopeVerbExecutesEndToEnd cannot fail")
	}

	// And the enum discovery: an empty case set must be caught rather than vacuously passing.
	if len(verbCases()) < len(broker.ValidOps()) {
		t.Fatalf("verbCases() covers %d of %d ops", len(verbCases()), len(broker.ValidOps()))
	}
}
