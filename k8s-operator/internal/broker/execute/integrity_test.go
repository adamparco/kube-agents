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

package execute

import (
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func diffOf(ops ...agentv1alpha1.AppliedDiffOp) DiffResult {
	return DiffResult{Ops: ops, TotalOps: len(ops)}
}

func op(kind, path string) agentv1alpha1.AppliedDiffOp {
	return agentv1alpha1.AppliedDiffOp{Op: kind, Path: path}
}

func TestIntegrityAcceptsTheClassifiedChange(t *testing.T) {
	c := Classified{TargetIndex: 0, Verb: "apply", TouchedPaths: []string{"/spec/replicas"}}
	if err := CheckIntegrity(c, diffOf(op("replace", "/spec/replicas"))); err != nil {
		t.Fatalf("CheckIntegrity: %v", err)
	}
}

func TestIntegrityAcceptsAMoreSpecificServerAnswer(t *testing.T) {
	// The classifier saw a change to the pod template; the server resolves it to the image inside.
	// Same change, described more precisely.
	c := Classified{Verb: "apply", TouchedPaths: []string{"/spec/template"}}
	err := CheckIntegrity(c, diffOf(op("replace", "/spec/template/spec/containers/0/image")))
	if err != nil {
		t.Fatalf("CheckIntegrity: %v", err)
	}
}

func TestIntegrityRefusesAWiderServerAnswer(t *testing.T) {
	// The reverse is not symmetric: the classifier saw `/spec/template`, the server would replace
	// the whole of `/spec`. That is a bigger change than the one that was classified.
	c := Classified{Verb: "apply", TouchedPaths: []string{"/spec/template"}}
	err := CheckIntegrity(c, diffOf(op("replace", "/spec")))
	if err == nil {
		t.Fatal("a server change wider than the classified one passed")
	}
}

func TestIntegrityCatchesTheExpandingPatch(t *testing.T) {
	// V-BRK-020's named attack. The classifier was shown a replica change and the server's dry run
	// says the merge would also swap the image -- the difference between `routine` and `gated`.
	c := Classified{TargetIndex: 2, Verb: "patch", TouchedPaths: []string{"/spec/replicas"}}
	err := CheckIntegrity(c, diffOf(
		op("replace", "/spec/replicas"),
		op("replace", "/spec/template/spec/containers/0/image"),
	))
	if err == nil {
		t.Fatal("an expanding patch passed the integrity check")
	}
	if !strings.Contains(err.Error(), "/spec/template/spec/containers/0/image") {
		t.Fatalf("the error does not name the offending path: %v", err)
	}
	if !strings.Contains(err.Error(), "target 2") {
		t.Fatalf("the error does not name the target: %v", err)
	}
}

func TestIntegrityMatchesOnTokenBoundaries(t *testing.T) {
	// A string-prefix comparison would accept this: "/spec/replicasHistory" starts with
	// "/spec/replicas".
	c := Classified{Verb: "apply", TouchedPaths: []string{"/spec/replicas"}}
	if err := CheckIntegrity(c, diffOf(op("replace", "/spec/replicasHistory"))); err == nil {
		t.Fatal("/spec/replicasHistory was accepted as covered by /spec/replicas")
	}
}

func TestIntegrityRefusesATruncatedDiff(t *testing.T) {
	// The violation could be in the part that was dropped.
	c := Classified{Verb: "apply", TouchedPaths: []string{"/spec/replicas"}}
	d := diffOf(op("replace", "/spec/replicas"))
	d.Truncated = true
	d.TotalOps = MaxDiffOps + 1

	err := CheckIntegrity(c, d)
	if err == nil {
		t.Fatal("a truncated diff was compared and passed")
	}
	if !strings.Contains(err.Error(), "refused") {
		t.Fatalf("the error should say the action is refused, not that the diff is long: %v", err)
	}
}

func TestIntegrityRefusesTheEmptyPointerAsAPath(t *testing.T) {
	// "" is the whole document, so accepting it would make every later comparison pass. This is the
	// vacuity guard: the one outcome the function must never produce by accident is "everything is
	// covered".
	c := Classified{Verb: "apply", TouchedPaths: []string{""}}
	if err := CheckIntegrity(c, diffOf(op("replace", "/spec/template"))); err == nil {
		t.Fatal("the empty pointer was accepted as a classified path")
	}
}

func TestIntegrityRefusesUnclassifiedChange(t *testing.T) {
	c := Classified{Verb: "apply"}
	if err := CheckIntegrity(c, diffOf(op("replace", "/spec/replicas"))); err == nil {
		t.Fatal("a change with no classified paths at all passed")
	}
}

func TestIntegrityAllowsANoOpApply(t *testing.T) {
	// An agent re-asserting desired state changes nothing and was classified as changing nothing.
	if err := CheckIntegrity(Classified{Verb: "apply"}, diffOf()); err != nil {
		t.Fatalf("a no-op apply was refused: %v", err)
	}
}

func TestIntegrityWholeObjectCreate(t *testing.T) {
	c := Classified{Verb: "create", WholeObject: true}
	if err := CheckIntegrity(c, diffOf(op("add", "/metadata/name"), op("add", "/spec/replicas"))); err != nil {
		t.Fatalf("a create of a new object was refused: %v", err)
	}

	// A replace means the object already existed: the thing about to be executed is an overwrite,
	// not the create that was classified.
	err := CheckIntegrity(c, diffOf(op("add", "/spec/replicas"), op("replace", "/spec/template")))
	if err == nil {
		t.Fatal("a create whose diff overwrites existing fields passed")
	}
}

func TestIntegrityWholeObjectDelete(t *testing.T) {
	c := Classified{Verb: "delete", WholeObject: true}
	if err := CheckIntegrity(c, diffOf(op("remove", "/spec/replicas"))); err != nil {
		t.Fatalf("a delete was refused: %v", err)
	}
	if err := CheckIntegrity(c, diffOf(op("replace", "/spec/replicas"))); err == nil {
		t.Fatal("a delete whose diff replaces a field passed")
	}
}

func TestIntegrityWholeObjectIsNotAnEscapeHatch(t *testing.T) {
	// Only create and delete are whole-object. A field-level verb arriving with WholeObject set and
	// no paths would otherwise be checked by nothing at all -- which is the shape of every vacuous
	// control: it passes, and it passes for the case it was written to catch.
	for _, verb := range []string{"apply", "patch", "scale"} {
		c := Classified{Verb: verb, WholeObject: true}
		if err := CheckIntegrity(c, diffOf(op("replace", "/spec/template"))); err == nil {
			t.Errorf("verb %q passed as a whole-object change", verb)
		}
	}
}

func TestCoveredBy(t *testing.T) {
	classified := []string{"/spec/replicas", "/metadata/annotations/kube-agents~1restarted-at"}
	cases := map[string]bool{
		"/spec/replicas":        true,
		"/spec/replicas/0":      true,
		"/spec/replicasHistory": false,
		"/spec":                 false,
		"/metadata/annotations/kube-agents~1restarted-at": true,
		"/metadata/annotations/other":                     false,
		"/metadata/annotations":                           false,
	}
	for pointer, want := range cases {
		if got := coveredBy(pointer, classified); got != want {
			t.Errorf("coveredBy(%q) = %v, want %v", pointer, got, want)
		}
	}
}
