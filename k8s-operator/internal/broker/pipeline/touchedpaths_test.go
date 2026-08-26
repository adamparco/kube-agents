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
	"fmt"
	"reflect"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
)

// V-BRK-020, at the seam rather than through the whole pipeline.
//
// pipeline_test.go asserts the end-to-end consequences -- an apply executes, a fieldPaths rule
// fires on it, an over-wide server answer is refused. This file asserts the conversion those
// consequences rest on, because a path set can be wrong in ways an end-to-end test cannot
// distinguish: reporting the whole object as touched makes a fieldPaths rule fire just as reliably
// as reporting the one field that changed, and only one of the two is a classification.

func liveCM(data map[string]any) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata":   map[string]any{"name": "app-config", "namespace": testTenantNS},
		"data":       data,
	}}
}

func desiredCM(data map[string]any) map[string]any {
	return map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata":   map[string]any{"name": "app-config", "namespace": testTenantNS},
		"data":       data,
	}
}

// TestFillTouchedPathsGivesEachVerbItsOwnFieldSet.
//
// One table over every verb the envelope's enum admits, because the failure this closes was a verb
// falling through to "no fields touched" and nobody noticing -- and a per-verb test that only
// covered the verbs somebody remembered would fail the same way.
func TestFillTouchedPathsGivesEachVerbItsOwnFieldSet(t *testing.T) {
	replicas := int32(3)

	for _, tc := range []struct {
		what string
		op   broker.Operation
		live *unstructured.Unstructured
		want []classify.PatchOp
	}{
		{
			what: "an apply that changes one field touches one field",
			op: broker.Operation{
				Op:           "apply",
				DesiredState: desiredCM(map[string]any{"log-level": "debug"}),
			},
			live: liveCM(map[string]any{"log-level": "info"}),
			want: []classify.PatchOp{{Op: "replace", Path: "/data/log-level", Value: "debug"}},
		},
		{
			what: "an apply that re-asserts live state touches nothing",
			op: broker.Operation{
				Op:           "apply",
				DesiredState: desiredCM(map[string]any{"log-level": "info"}),
			},
			live: liveCM(map[string]any{"log-level": "info"}),
			want: []classify.PatchOp{},
		},
		{
			what: "an apply over an absent object touches every field it sets",
			op: broker.Operation{
				Op:           "apply",
				DesiredState: desiredCM(map[string]any{"log-level": "info"}),
			},
			live: nil,
			want: []classify.PatchOp{
				{Op: "add", Path: "/apiVersion", Value: "v1"},
				{Op: "add", Path: "/data/log-level", Value: "info"},
				{Op: "add", Path: "/kind", Value: "ConfigMap"},
				{Op: "add", Path: "/metadata/name", Value: "app-config"},
				{Op: "add", Path: "/metadata/namespace", Value: testTenantNS},
			},
		},
		{
			what: "an apply that removes a field reports the removal with no value",
			op: broker.Operation{
				Op:           "apply",
				DesiredState: desiredCM(map[string]any{"log-level": "info"}),
			},
			live: liveCM(map[string]any{"log-level": "info", "owner": "team-a"}),
			// Value stays nil: nothing is being set, so there is nothing to scan for secret
			// material and nothing for the boolean direction analysis to read.
			want: []classify.PatchOp{{Op: "remove", Path: "/data/owner"}},
		},
		{
			what: "an apply patch is diffed like the apply it is",
			op: broker.Operation{
				Op: "patch",
				Patch: &broker.Patch{
					Type: mediaApplyPatch,
					Body: desiredCM(map[string]any{"log-level": "debug"}),
				},
			},
			live: liveCM(map[string]any{"log-level": "info"}),
			want: []classify.PatchOp{{Op: "replace", Path: "/data/log-level", Value: "debug"}},
		},
		{
			what: "a merge patch touches its leaves, and a null is a removal",
			op: broker.Operation{
				Op: "patch",
				Patch: &broker.Patch{
					Type: "application/merge-patch+json",
					Body: map[string]any{"data": map[string]any{"log-level": "debug", "owner": nil}},
				},
			},
			live: liveCM(map[string]any{"log-level": "info", "owner": "team-a"}),
			want: []classify.PatchOp{
				{Op: "replace", Path: "/data/log-level", Value: "debug"},
				{Op: "remove", Path: "/data/owner"},
			},
		},
		{
			what: "a scale touches the replica count and nothing else",
			op:   broker.Operation{Op: "scale", Scale: &broker.ScaleSpec{Replicas: &replicas}},
			live: liveCM(map[string]any{"log-level": "info"}),
			// The pointer is written out rather than referenced as scaleReplicasPointer. Naming the
			// constant would compare it against itself: a mutation sweep changed it to
			// `/spec/replica` and this table stayed green, because both sides moved together. The
			// value under test is what a rule author writes in `when.fieldPaths`, so the test has
			// to spell it the way they would.
			want: []classify.PatchOp{{Op: "replace", Path: "/spec/replicas", Value: int64(3)}},
		},
		{
			what: "a create is a whole-object verb and gets no path set",
			op: broker.Operation{
				Op:           "create",
				DesiredState: desiredCM(map[string]any{"log-level": "info"}),
			},
			live: nil,
			want: nil,
		},
		{
			what: "a delete is a whole-object verb and gets no path set",
			op:   broker.Operation{Op: "delete"},
			live: liveCM(map[string]any{"log-level": "info"}),
			want: nil,
		},
	} {
		t.Run(tc.what, func(t *testing.T) {
			env := &broker.Envelope{Operations: []broker.Operation{tc.op}}
			raws := []classify.RawOp{{Verb: tc.op.Op}}
			snaps := []execute.Snapshot{{TargetIndex: 0, Live: tc.live, Existed: tc.live != nil}}

			if err := fillTouchedPaths(env, raws, snaps); err != nil {
				t.Fatalf("fillTouchedPaths: %v", err)
			}
			if !reflect.DeepEqual(raws[0].Patch, tc.want) {
				t.Fatalf("patch ops =\n  %#v\nwant\n  %#v", raws[0].Patch, tc.want)
			}
			// The paths the classifier will actually match rules against, derived the same way
			// classify.Resolve derives them. Asserting the op list alone would not catch a path
			// that TouchedPaths drops.
			if got := len(classify.TouchedPaths(raws[0].Patch)); got != len(tc.want) {
				t.Errorf("TouchedPaths yielded %d pointers from %d ops", got, len(tc.want))
			}
		})
	}
}

// TestAJSONPatchIsLeftAlone.
//
// patchOps already read it, at a point where live state was not available. fillTouchedPaths
// overwriting it would replace the operations the agent actually submitted with a diff of an
// object the agent never sent -- and for a `move` or a `test` op there is no such object.
func TestAJSONPatchIsLeftAlone(t *testing.T) {
	env := &broker.Envelope{Operations: []broker.Operation{{
		Op:    "patch",
		Patch: &broker.Patch{Type: mediaJSONPatch, Body: []any{}},
	}}}
	submitted := []classify.PatchOp{
		{Op: "move", Path: "/data/new", From: "/data/old"},
		{Op: "remove", Path: "/data/stale"},
	}
	raws := []classify.RawOp{{Verb: "patch", Patch: submitted}}
	snaps := []execute.Snapshot{{TargetIndex: 0, Live: liveCM(map[string]any{"old": "x", "stale": "y"})}}

	if err := fillTouchedPaths(env, raws, snaps); err != nil {
		t.Fatalf("fillTouchedPaths: %v", err)
	}
	if !reflect.DeepEqual(raws[0].Patch, submitted) {
		t.Fatalf("a submitted JSON Patch was rewritten:\n  got  %#v\n  want %#v", raws[0].Patch, submitted)
	}
	// The `from` end of the move must survive, because 06 §4.2 counts it as touched.
	if got := classify.TouchedPaths(raws[0].Patch); len(got) != 3 {
		t.Errorf("TouchedPaths = %v, want the move's both ends plus the remove", got)
	}
}

// TestTheClassifierSeesTheVALUEAndNotItsRendering is the property LSN-040 was opened for.
//
// execute.DiffResult renders every value as a string, because its other consumer is an ActionRecord
// field with a length bound. The classifier's secret scan walks structured payloads and
// DirectionOfBoolField asks whether a value IS the bool true. Hand it the diff's rendering and both
// go quiet: a scan of the string "true" finds no bool, and `privileged: true` -- the single most
// consequential field an agent can set -- stops being read as a loosening.
//
// This is why the conversion takes paths from the diff and values from the desired object, rather
// than taking the diff wholesale.
func TestTheClassifierSeesTheVALUEAndNotItsRendering(t *testing.T) {
	desired := map[string]any{
		"apiVersion": "v1",
		"kind":       "Pod",
		"metadata":   map[string]any{"name": "p", "namespace": testTenantNS},
		"spec": map[string]any{
			"securityContext": map[string]any{
				"privileged":   true,
				"runAsUser":    int64(0),
				"runAsNonRoot": false,
			},
		},
	}
	env := &broker.Envelope{Operations: []broker.Operation{{Op: "apply", DesiredState: desired}}}
	raws := []classify.RawOp{{Verb: "apply"}}
	snaps := []execute.Snapshot{{TargetIndex: 0}}

	if err := fillTouchedPaths(env, raws, snaps); err != nil {
		t.Fatalf("fillTouchedPaths: %v", err)
	}

	byPath := map[string]any{}
	for _, p := range raws[0].Patch {
		byPath[p.Path] = p.Value
	}
	for path, want := range map[string]any{
		"/spec/securityContext/privileged":   true,
		"/spec/securityContext/runAsNonRoot": false,
		"/spec/securityContext/runAsUser":    int64(0),
	} {
		got, present := byPath[path]
		if !present {
			t.Fatalf("no op at %s; the paths are %v", path, classify.TouchedPaths(raws[0].Patch))
		}
		if got != want || reflect.TypeOf(got) != reflect.TypeOf(want) {
			t.Errorf("value at %s = %#v (%T), want %#v (%T); a rendered value is not a value",
				path, got, got, want, want)
		}
	}

	// And the consequence, asserted through the analysis rather than restated: a bool the diff had
	// rendered as a string would produce no direction at all.
	if d := classify.CombineDirection(nil); d == classify.DirectionLoosen {
		t.Fatal("the empty direction is already loosen, so the next assertion proves nothing")
	}
	var loosens bool
	for _, p := range raws[0].Patch {
		b, isBool := p.Value.(bool)
		if !isBool {
			continue
		}
		if dir, known := classify.DirectionOfBoolField(p.Path[strings.LastIndex(p.Path, "/")+1:], b); known && dir == classify.DirectionLoosen {
			loosens = true
		}
	}
	if !loosens {
		t.Error("no boolean field read as a loosening; privileged=true must, or the direction analysis is blind to the apply verb")
	}
}

// TestAKeyContainingASlashSurvivesTheRoundTrip.
//
// Annotation keys are domain-prefixed paths and routinely contain `/`. The diff escapes them per
// RFC 6901 and the value lookup has to unescape with the SAME implementation, which is why
// classify.SplitPointer is exported rather than reimplemented at this seam. A second unescaper
// would agree until the first such key, then hand the classifier a nil value for a field that is
// being set -- and a nil value is not scanned for secret material.
func TestAKeyContainingASlashSurvivesTheRoundTrip(t *testing.T) {
	const key = "kube-agents.io/managed-by"
	desired := map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":        "app-config",
			"namespace":   testTenantNS,
			"annotations": map[string]any{key: "platform"},
		},
	}
	env := &broker.Envelope{Operations: []broker.Operation{{Op: "apply", DesiredState: desired}}}
	raws := []classify.RawOp{{Verb: "apply"}}

	if err := fillTouchedPaths(env, raws, []execute.Snapshot{{TargetIndex: 0}}); err != nil {
		t.Fatalf("fillTouchedPaths: %v", err)
	}

	want := classify.JoinPointer("metadata", "annotations", key)
	for _, p := range raws[0].Patch {
		if p.Path != want {
			continue
		}
		if p.Value != "platform" {
			t.Fatalf("value at %s = %#v, want %q; the pointer was escaped by one implementation and read by another",
				want, p.Value, "platform")
		}
		return
	}
	t.Fatalf("no op at %s; the paths are %v", want, classify.TouchedPaths(raws[0].Patch))
}

// TestAChangeTooLargeToClassifyIsRefusedRatherThanTruncated.
//
// execute.CheckIntegrity already refuses a truncated diff at the far end of the pipeline, for the
// reason stated there: the violation could be among the operations that were dropped. The same
// reasoning binds here and points the same way. A path set that is a PREFIX of the real one
// understates the change, and a fieldPaths rule that would have fired on the field past the cap
// silently does not -- so truncating would be a loosening that arrives without a message.
func TestAChangeTooLargeToClassifyIsRefusedRatherThanTruncated(t *testing.T) {
	data := map[string]any{}
	for i := 0; i <= execute.MaxDiffOps; i++ {
		data[fmt.Sprintf("key-%03d", i)] = "v"
	}
	env := &broker.Envelope{Operations: []broker.Operation{{Op: "apply", DesiredState: desiredCM(data)}}}
	raws := []classify.RawOp{{Verb: "apply"}}

	err := fillTouchedPaths(env, raws, []execute.Snapshot{{TargetIndex: 0}})
	if err == nil {
		t.Fatalf("a change of %d fields was classified on a prefix of itself", len(data))
	}
	var ref *broker.Refusal
	if !errors.As(err, &ref) {
		t.Fatalf("error is %T, want a *broker.Refusal so the caller journals it rather than 500ing: %v", err, err)
	}
	if ref.Reason != "change-too-large-to-classify" {
		t.Errorf("reason = %q", ref.Reason)
	}
	if !ref.Journal {
		t.Error("the refusal is not journalled; an action refused for being unclassifiable is exactly the one a human needs to see")
	}
	if raws[0].Patch != nil {
		t.Error("the refused operation was still given a path set, which a caller ignoring the error would classify against")
	}
}
