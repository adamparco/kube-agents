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
	"fmt"
	"strings"
)

// Classify/execute integrity -- V-BRK-020.
//
// The gate at step 4 decides on a diff. The apply at step 9 sends a payload. Those are two different
// objects, and everything the classifier concluded is worthless if the second one does more than the
// first one described.
//
// The concrete attack the spec names is a strategic-merge patch that EXPANDS: a payload whose
// server-side merge touches fields the submitted patch never mentions. The agent submits something
// that classifies `routine`, the server applies something that would have classified `gated`, and
// every record in the journal says `routine` -- accurately, because that is genuinely what the
// classifier was shown. Nothing in the pipeline notices, because each step did its job on the input
// it was given.
//
// So the executed diff is computed from the SERVER's dry-run answer (what it says the object will
// become), not from the payload, and compared against the paths the classifier actually saw.

// Classified is what the classifier was shown for one operation. It is passed forward rather than
// recomputed, because a recomputation is a second answer to the same question, and the whole point
// of the check is to compare against the FIRST one.
type Classified struct {
	// TargetIndex is the position in the envelope's target list.
	TargetIndex int

	// Verb is the envelope op.
	Verb string

	// WholeObject marks create/delete, where "which fields changed" has no meaningful answer and
	// TouchedPaths is empty by construction (classify.ResolvedOp.WholeObject).
	WholeObject bool

	// TouchedPaths is `classify.ResolvedOp.TouchedPaths` verbatim: RFC 6901 pointers, produced by
	// this package's Diff over (live, desired) before classification ran.
	TouchedPaths []string
}

// CheckIntegrity refuses an execution whose effect exceeds what was classified.
//
// It returns an error rather than a bool with a reason, because there is exactly one correct
// response to a violation -- do not execute -- and a bool invites a caller to log it.
func CheckIntegrity(c Classified, executed DiffResult) error {
	if executed.Truncated {
		// A truncated diff cannot support the claim. The violating path could be among the ones that
		// were dropped, and "the first two hundred operations were all covered" is not the property.
		return fmt.Errorf(
			"target %d (%s): the executed diff has %d operations, over the %d the broker records, so it cannot be compared against the classified change; the action is refused rather than partially checked",
			c.TargetIndex, c.Verb, executed.TotalOps, MaxDiffOps)
	}

	if c.WholeObject {
		return checkWholeObject(c, executed)
	}

	if len(c.TouchedPaths) == 0 {
		if len(executed.Ops) == 0 {
			// Classified as touching nothing, and the server agrees it would change nothing. That is
			// a no-op apply, which is legal and common (an agent re-asserting desired state).
			return nil
		}
		return fmt.Errorf(
			"target %d (%s): the classifier was shown no changed fields, but the server would change %d (%s); nothing about this action was classified",
			c.TargetIndex, c.Verb, len(executed.Ops), strings.Join(executed.Pointers(), ", "))
	}

	for _, p := range c.TouchedPaths {
		if p == "" {
			// The empty pointer is the whole document. Accepting it here would make every subsequent
			// comparison pass, which is the one outcome this function must never produce by accident.
			return fmt.Errorf(
				"target %d (%s): the classified path set contains the empty pointer, which covers every field; a whole-object change must be declared with WholeObject, not smuggled in as a path",
				c.TargetIndex, c.Verb)
		}
	}

	for _, got := range executed.Pointers() {
		if !coveredBy(got, c.TouchedPaths) {
			return fmt.Errorf(
				"target %d (%s): the server would change %s, which is not among the fields the classifier saw (%s); the executed change exceeds the classified change",
				c.TargetIndex, c.Verb, got, strings.Join(c.TouchedPaths, ", "))
		}
	}
	return nil
}

// checkWholeObject is the create/delete case.
//
// There are no paths to compare, so the check asserts the SHAPE the verb implies. It exists because
// the alternative -- returning nil for whole-object verbs -- would make V-BRK-020 vacuous for
// exactly the two verbs with the largest effect.
func checkWholeObject(c Classified, executed DiffResult) error {
	switch c.Verb {
	case "create":
		// A create's diff is against nothing, so every operation must be an add. A `replace` or a
		// `remove` means the object already existed, i.e. the thing being executed is not the thing
		// that was classified.
		for _, op := range executed.Ops {
			if op.Op != "add" {
				return fmt.Errorf(
					"target %d (create): the server reports %s at %s, so the object already exists; a create was classified but an overwrite would be executed",
					c.TargetIndex, op.Op, op.Path)
			}
		}
	case "delete":
		for _, op := range executed.Ops {
			if op.Op != "remove" {
				return fmt.Errorf(
					"target %d (delete): the server reports %s at %s; a delete's diff can only remove",
					c.TargetIndex, op.Op, op.Path)
			}
		}
	default:
		return fmt.Errorf(
			"target %d: verb %q was classified as a whole-object change, which only create and delete are; a field-level verb with no path set was not checked by anything",
			c.TargetIndex, c.Verb)
	}
	return nil
}

// coveredBy reports whether a pointer is one of, or beneath, the classified pointers.
//
// Beneath, not merely equal: the classifier is shown the diff of the SUBMITTED change, and the
// server's answer can legitimately be more specific -- a submitted change at `/spec/template` whose
// merge resolves to `/spec/template/spec/containers/0/image` is the same change described more
// precisely. The reverse is not accepted: a server change at `/spec` when the classifier saw
// `/spec/template` is a wider change than the one that was classified.
//
// Comparison is on token boundaries, so `/spec/replicasHistory` is not covered by `/spec/replicas`.
func coveredBy(pointer string, classified []string) bool {
	for _, c := range classified {
		if pointer == c || strings.HasPrefix(pointer, c+"/") {
			return true
		}
	}
	return false
}
