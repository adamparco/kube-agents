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
	"encoding/json"
	"fmt"
	"sort"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// The one diff in the broker, used at both ends of the pipeline.
//
// It is called twice per action and the two calls are the whole of V-BRK-020:
//
//  1. BEFORE classification, over (live state, desired state), to fill
//     `classify.ResolvedOp.TouchedPaths` -- so an apply that changes one field is classified as
//     touching one field rather than the whole object (06 §4.2).
//  2. AFTER the server-side-apply DRY RUN, over (live state, what the server says it would become),
//     to answer the only question that matters at step 9: is the change the server is about to make
//     the change the classifier looked at?
//
// One function, called twice, is the design. Two functions -- a "what we intend to change" estimate
// and an "what actually changed" report -- would drift, and the drift would appear as an integrity
// violation on honest actions and as silence on the dishonest one, because the dishonest case is
// precisely a payload whose server-side expansion the estimating function did not model.
const (
	// MaxDiffOps caps the recorded diff. A create renders the whole object, and an `ActionRecord`
	// is an etcd object with a size limit it shares with everything else in the namespace.
	MaxDiffOps = 200

	// MaxRenderedValueBytes caps one rendered value. Long enough for an image reference, a resource
	// quantity or a CIDR; short enough that a ConfigMap holding a 400 KiB file does not become 400
	// KiB of journal.
	MaxRenderedValueBytes = 512
)

// DiffResult is a normalized JSON-patch plus what had to be left out of it.
type DiffResult struct {
	// Ops is the patch, sorted by path so two runs over the same pair of objects are byte-identical.
	Ops []agentv1alpha1.AppliedDiffOp

	// Truncated reports that the diff exceeded MaxDiffOps and Ops is a prefix.
	//
	// A bool rather than a silent cut. A truncated diff is still usable evidence for a human, but it
	// is NOT usable for the integrity check -- a violation could be in the part that was dropped --
	// so CheckIntegrity refuses on it rather than passing an incomplete comparison.
	Truncated bool

	// TotalOps is how many operations the full diff would have had.
	TotalOps int
}

// Pointers returns the RFC 6901 pointer of every op, in order.
func (d DiffResult) Pointers() []string {
	out := make([]string, 0, len(d.Ops))
	for _, op := range d.Ops {
		out = append(out, op.Path)
	}
	return out
}

// Diff computes the normalized patch from `before` to `after`.
//
// BOTH SIDES ARE SANITIZED FIRST, and the caller cannot opt out. The sanitizer (06 §4.3.1) drops the
// server-owned metadata that would otherwise make every diff report a `resourceVersion` change, and
// -- the reason this is not a parameter -- it replaces Secret values with digests. The `after` side
// of an apply is the agent's desired state, which for a Secret contains the plaintext. A diff is
// written into the `ActionRecord`, which is read by the chat report and the audit export, so an
// opt-in sanitizer here is an opt-out sanitizer for whoever forgets, on the one object class where
// forgetting copies a credential into three places at once.
//
// A nil side is the empty object: `Diff(nil, obj)` is a create and `Diff(obj, nil)` is a delete.
func Diff(before, after *unstructured.Unstructured) (DiffResult, error) {
	b, err := sanitizedMap(before)
	if err != nil {
		return DiffResult{}, fmt.Errorf("diff: sanitizing the prior state: %w", err)
	}
	a, err := sanitizedMap(after)
	if err != nil {
		return DiffResult{}, fmt.Errorf("diff: sanitizing the resulting state: %w", err)
	}

	var ops []agentv1alpha1.AppliedDiffOp
	diffMaps(nil, b, a, &ops)

	sort.Slice(ops, func(i, j int) bool {
		if ops[i].Path != ops[j].Path {
			return ops[i].Path < ops[j].Path
		}
		return ops[i].Op < ops[j].Op
	})

	res := DiffResult{TotalOps: len(ops)}
	if len(ops) > MaxDiffOps {
		res.Truncated = true
		ops = ops[:MaxDiffOps]
	}
	res.Ops = ops
	return res, nil
}

// sanitizedMap runs the shared sanitizer and returns the object's content.
func sanitizedMap(obj *unstructured.Unstructured) (map[string]any, error) {
	if obj == nil {
		return map[string]any{}, nil
	}
	// isStatusTarget=false: a diff is about the spec change the agent made. A status-subresource
	// action carries its own snapshot through the undo package, which is where that flag belongs.
	clean, _, err := undo.Sanitize(obj, false)
	if err != nil {
		return nil, err
	}
	return clean.Object, nil
}

// diffMaps walks two maps in parallel, appending ops for every difference.
//
// Lists are compared as WHOLE VALUES rather than element-wise, and that is deliberate. Element-wise
// diffing of a Kubernetes list needs the merge key from the OpenAPI schema; guessing it produces a
// diff that is confidently wrong (a container inserted at position 0 renders as every container
// having changed). Because the same function computes both sides of the integrity comparison, the
// coarsening cannot manufacture a false pass -- it makes both the classified set and the executed
// set coarse in exactly the same way.
func diffMaps(prefix []string, before, after map[string]any, ops *[]agentv1alpha1.AppliedDiffOp) {
	keys := make([]string, 0, len(before)+len(after))
	seen := map[string]bool{}
	for k := range before {
		keys = append(keys, k)
		seen[k] = true
	}
	for k := range after {
		if !seen[k] {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)

	for _, k := range keys {
		path := append(append([]string{}, prefix...), k)
		bv, inBefore := before[k]
		av, inAfter := after[k]

		switch {
		case inBefore && !inAfter:
			if m, ok := bv.(map[string]any); ok {
				if sub := subtreeOps(path, m, nil); len(sub) > 0 {
					*ops = append(*ops, sub...)
					continue
				}
			}
			*ops = append(*ops, agentv1alpha1.AppliedDiffOp{
				Op: "remove", Path: classify.JoinPointer(path...), From: render(bv),
			})
		case !inBefore && inAfter:
			if m, ok := av.(map[string]any); ok {
				if sub := subtreeOps(path, nil, m); len(sub) > 0 {
					*ops = append(*ops, sub...)
					continue
				}
			}
			*ops = append(*ops, agentv1alpha1.AppliedDiffOp{
				Op: "add", Path: classify.JoinPointer(path...), Value: render(av),
			})
		default:
			bm, bIsMap := bv.(map[string]any)
			am, aIsMap := av.(map[string]any)
			if bIsMap && aIsMap {
				diffMaps(path, bm, am, ops)
				continue
			}
			if equalValues(bv, av) {
				continue
			}
			*ops = append(*ops, agentv1alpha1.AppliedDiffOp{
				Op: "replace", Path: classify.JoinPointer(path...), From: render(bv), Value: render(av),
			})
		}
	}
}

// subtreeOps expands a wholly-added or wholly-removed map into its LEAF operations.
//
// The coarse alternative -- one `add` at `/metadata/annotations` when the annotations map did not
// exist before -- is consistent (the same function computes both sides of the integrity check) and
// still WRONG, because the diff is also what `when.fieldPaths` is matched against. A rule naming
// `metadata.annotations['kube-agents/restarted-at']` is longer than `/metadata/annotations`, and
// PointerPrefixMatch requires the RULE to be the prefix, so the rule would not fire. That turns
// "create the whole annotations block in one apply" into a way to set a gated annotation without
// tripping the gate -- and it would fail silently, which is the failure mode 06 §4.2 is written
// around.
//
// A subtree that expands to nothing (an empty map added) falls back to the whole-value op, so that
// `annotations: {}` still appears in the diff rather than vanishing from it.
func subtreeOps(prefix []string, before, after map[string]any) []agentv1alpha1.AppliedDiffOp {
	if before == nil {
		before = map[string]any{}
	}
	if after == nil {
		after = map[string]any{}
	}
	var sub []agentv1alpha1.AppliedDiffOp
	diffMaps(prefix, before, after, &sub)
	return sub
}

// equalValues compares two decoded JSON values.
//
// Via their marshalled form, because the two sides come from different places: the live object was
// decoded from the API server's JSON, the desired object may have been built in Go, and an int64 5
// and a float64 5 are the same number in every sense that matters to a Kubernetes object while being
// different to reflect.DeepEqual. Reporting `replicas: 5 -> 5` as a change would put a spurious
// path into the executed diff and fail the integrity check on an action that changed nothing.
func equalValues(a, b any) bool {
	ab, aErr := json.Marshal(a)
	bb, bErr := json.Marshal(b)
	if aErr != nil || bErr != nil {
		return false
	}
	return string(ab) == string(bb)
}

// render turns a decoded JSON value into the string the record carries, truncating loudly.
func render(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return truncateRendered(s)
	}
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprintf("<unrenderable %T>", v)
	}
	return truncateRendered(string(b))
}

func truncateRendered(s string) string {
	if len(s) <= MaxRenderedValueBytes {
		return s
	}
	// The marker names the byte count so a reader can tell a truncated value from one that happens
	// to end in an ellipsis.
	return fmt.Sprintf("%s…(%d bytes truncated)", s[:MaxRenderedValueBytes], len(s)-MaxRenderedValueBytes)
}
