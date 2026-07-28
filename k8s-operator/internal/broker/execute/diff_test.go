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
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

func deployment(mutate func(m map[string]any)) *unstructured.Unstructured {
	m := map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata": map[string]any{
			"name":      "api",
			"namespace": "team-a",
		},
		"spec": map[string]any{
			"replicas": int64(3),
			"template": map[string]any{
				"spec": map[string]any{
					"containers": []any{
						map[string]any{"name": "api", "image": "api:1.0"},
					},
				},
			},
		},
	}
	if mutate != nil {
		mutate(m)
	}
	return &unstructured.Unstructured{Object: m}
}

func pointersOf(t *testing.T, d DiffResult) []string {
	t.Helper()
	return d.Pointers()
}

func TestDiffScalarChange(t *testing.T) {
	before := deployment(nil)
	after := deployment(func(m map[string]any) {
		m["spec"].(map[string]any)["replicas"] = int64(5)
	})

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if len(d.Ops) != 1 {
		t.Fatalf("ops = %v, want exactly one", pointersOf(t, d))
	}
	op := d.Ops[0]
	if op.Path != "/spec/replicas" || op.Op != "replace" || op.From != "3" || op.Value != "5" {
		t.Fatalf("op = %+v, want replace /spec/replicas 3 -> 5", op)
	}
}

func TestDiffIntAndFloatAreTheSameNumber(t *testing.T) {
	// The live object is decoded from JSON (float64); a desired object built in Go carries int64.
	// Reporting that as a change would put a spurious path into the executed diff and fail the
	// integrity check on an action that changed nothing.
	before := deployment(func(m map[string]any) {
		m["spec"].(map[string]any)["replicas"] = float64(3)
	})
	after := deployment(nil) // int64(3)

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if len(d.Ops) != 0 {
		t.Fatalf("ops = %v, want none: int64 3 and float64 3 are the same replica count", pointersOf(t, d))
	}
}

func TestDiffNilSidesAreCreateAndDelete(t *testing.T) {
	obj := deployment(nil)

	created, err := Diff(nil, obj)
	if err != nil {
		t.Fatalf("Diff(nil, obj): %v", err)
	}
	if len(created.Ops) == 0 {
		t.Fatal("a create diffed to nothing")
	}
	for _, op := range created.Ops {
		if op.Op != "add" {
			t.Fatalf("create produced %s at %s, want only adds", op.Op, op.Path)
		}
	}

	deleted, err := Diff(obj, nil)
	if err != nil {
		t.Fatalf("Diff(obj, nil): %v", err)
	}
	for _, op := range deleted.Ops {
		if op.Op != "remove" {
			t.Fatalf("delete produced %s at %s, want only removes", op.Op, op.Path)
		}
	}
}

func TestDiffIsDeterministic(t *testing.T) {
	// Two runs over the same pair must be byte-identical: the diff is written into an ActionRecord
	// that a golden test and a human both read, and a map-iteration-ordered diff makes every record
	// differ from every other for no reason.
	before := deployment(nil)
	after := deployment(func(m map[string]any) {
		m["spec"].(map[string]any)["replicas"] = int64(5)
		m["metadata"].(map[string]any)["labels"] = map[string]any{"b": "2", "a": "1"}
		m["spec"].(map[string]any)["paused"] = true
	})

	var first string
	for i := 0; i < 20; i++ {
		d, err := Diff(before, after)
		if err != nil {
			t.Fatalf("Diff: %v", err)
		}
		got := fmt.Sprint(d.Ops)
		if i == 0 {
			first = got
			continue
		}
		if got != first {
			t.Fatalf("run %d differs:\n%s\n%s", i, first, got)
		}
	}
}

func TestDiffSanitizesSecretValues(t *testing.T) {
	// The caller cannot opt out, and this is why: the `after` side of a Secret apply is plaintext,
	// and the diff is written into a record that the chat report and the audit export both read.
	before := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "db", "namespace": "team-a"},
		"data":       map[string]any{"password": "b2xkLXBhc3N3b3Jk"}, // old-password
	}}
	after := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "db", "namespace": "team-a"},
		"data":       map[string]any{"password": "c3VwZXItc2VjcmV0"}, // super-secret
	}}

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	rendered := fmt.Sprint(d.Ops)
	for _, leak := range []string{"c3VwZXItc2VjcmV0", "super-secret", "b2xkLXBhc3N3b3Jk", "old-password"} {
		if strings.Contains(rendered, leak) {
			t.Fatalf("the diff carries %q:\n%s", leak, rendered)
		}
	}
	if len(d.Ops) == 0 {
		t.Fatal("the secret change diffed to nothing; a redaction that hides the change as well as the value is not a diff")
	}
}

func TestDiffDropsServerOwnedMetadata(t *testing.T) {
	// Every re-read of an object has a new resourceVersion. If that reached the diff, every action
	// would report a change to a field no agent touched, and the integrity check would fail on all
	// of them.
	before := deployment(func(m map[string]any) {
		m["metadata"].(map[string]any)["resourceVersion"] = "1000"
	})
	after := deployment(func(m map[string]any) {
		m["metadata"].(map[string]any)["resourceVersion"] = "1001"
	})

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if len(d.Ops) != 0 {
		t.Fatalf("ops = %v, want none", pointersOf(t, d))
	}
}

func TestDiffListsCompareWhole(t *testing.T) {
	before := deployment(nil)
	after := deployment(func(m map[string]any) {
		spec := m["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)
		spec["containers"] = []any{
			map[string]any{"name": "api", "image": "api:2.0"},
		}
	})

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	got := pointersOf(t, d)
	if len(got) != 1 || got[0] != "/spec/template/spec/containers" {
		t.Fatalf("ops = %v, want the whole containers list", got)
	}
}

func TestDiffEscapesPointerTokens(t *testing.T) {
	// The annotation key contains a slash. If the escaper here and the one classify's matcher
	// inverts ever diverge, a rule naming this annotation silently stops matching -- which is why
	// there is only one escaper and this test names its output literally.
	before := deployment(nil)
	after := deployment(func(m map[string]any) {
		m["metadata"].(map[string]any)["annotations"] = map[string]any{
			"kube-agents/restarted-at": "2026-07-27T00:00:00Z",
		}
	})

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	got := pointersOf(t, d)
	want := "/metadata/annotations/kube-agents~1restarted-at"
	if len(got) != 1 || got[0] != want {
		t.Fatalf("ops = %v, want [%s]", got, want)
	}
}

func TestDiffTruncatesLoudly(t *testing.T) {
	before := deployment(nil)
	after := deployment(func(m map[string]any) {
		labels := map[string]any{}
		for i := 0; i < MaxDiffOps+50; i++ {
			labels[fmt.Sprintf("k%03d", i)] = "v"
		}
		m["metadata"].(map[string]any)["labels"] = labels
	})

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if !d.Truncated {
		t.Fatal("a diff over the cap reported Truncated=false; a silent cut is an integrity check on a prefix")
	}
	if len(d.Ops) != MaxDiffOps {
		t.Fatalf("ops = %d, want %d", len(d.Ops), MaxDiffOps)
	}
	if d.TotalOps <= MaxDiffOps {
		t.Fatalf("TotalOps = %d, want the full count", d.TotalOps)
	}
}

func TestRenderTruncatesLongValues(t *testing.T) {
	long := strings.Repeat("x", MaxRenderedValueBytes*2)
	before := deployment(nil)
	after := deployment(func(m map[string]any) {
		m["metadata"].(map[string]any)["annotations"] = map[string]any{"blob": long}
	})

	d, err := Diff(before, after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if len(d.Ops) != 1 {
		t.Fatalf("ops = %v", pointersOf(t, d))
	}
	v := d.Ops[0].Value
	if len(v) >= len(long) {
		t.Fatalf("value is %d bytes, want it truncated", len(v))
	}
	if !strings.Contains(v, "bytes truncated") {
		t.Fatalf("value = %q, want a truncation marker naming the dropped byte count", v)
	}
}

// assertLeafOps is the mechanization of [[lsn-034]].
//
// Every op a diff emits must name a LEAF, never a subtree. The invariant is checkable from the op
// alone: if `Value` or `From` parses as a non-empty JSON object, the op is describing a whole
// subtree, and every field inside that subtree has a path LONGER than the op's. `when.fieldPaths`
// matching requires the RULE to be the prefix of the diff path, so a rule naming anything inside a
// subtree op cannot fire against it -- the change happened, the gate did not.
//
// An EMPTY object is allowed and is the deliberate fallback in subtreeOps: `annotations: {}` has no
// leaves, and dropping it would remove the change from the diff altogether. A JSON array is allowed
// too -- lists are compared and reported whole by design, documented at the top of diff.go.
func assertLeafOps(t *testing.T, d DiffResult) {
	t.Helper()
	for _, o := range d.Ops {
		for label, raw := range map[string]string{"value": o.Value, "from": o.From} {
			if raw == "" || strings.Contains(raw, "bytes truncated") {
				continue // truncateRendered leaves unparseable JSON; nothing to assert.
			}
			var decoded any
			if err := json.Unmarshal([]byte(raw), &decoded); err != nil {
				continue // a bare string, not JSON.
			}
			m, ok := decoded.(map[string]any)
			if ok && len(m) > 0 {
				t.Errorf("op %s at %q carries a non-empty object as its %s (%q): "+
					"a rule naming a field inside it is longer than this path and cannot prefix-match it",
					o.Op, o.Path, label, raw)
			}
		}
	}
}

func TestDiffEmitsOnlyLeafOps(t *testing.T) {
	// A wholly-new subtree several levels deep, on both sides. The coarse implementation emitted one
	// `add /spec/template/spec/tolerations`-shaped op for each; a `when.fieldPaths` rule naming
	// anything inside would then never match.
	nested := map[string]any{
		"metadata": map[string]any{
			"annotations": map[string]any{
				"kube-agents/restarted-at": "2026-07-27T00:00:00Z",
				"nested": map[string]any{
					"deeper": map[string]any{"leaf": "v"},
				},
			},
		},
		"spec": map[string]any{
			"strategy": map[string]any{
				"rollingUpdate": map[string]any{"maxSurge": int64(1), "maxUnavailable": int64(0)},
			},
		},
	}

	added := deployment(func(m map[string]any) { mergeInto(m, nested) })

	// Added.
	d, err := Diff(deployment(nil), added)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	assertLeafOps(t, d)
	wantAdded := "/metadata/annotations/nested/deeper/leaf"
	if !hasPointer(d, wantAdded) {
		t.Errorf("pointers = %v, want one of them to be %s", pointersOf(t, d), wantAdded)
	}

	// Removed -- the same expansion must happen on the other arm of the switch, which is a separate
	// code path and was separately wrong.
	d, err = Diff(added, deployment(nil))
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	assertLeafOps(t, d)
	if !hasPointer(d, wantAdded) {
		t.Errorf("pointers = %v, want one of them to be %s", pointersOf(t, d), wantAdded)
	}
	for _, o := range d.Ops {
		if o.Op != "remove" {
			t.Errorf("op at %q is %q, want remove", o.Path, o.Op)
		}
	}
}

func TestDiffKeepsAnEmptyMapVisible(t *testing.T) {
	// The fallback in subtreeOps. An empty map expands to no leaves, so recursing alone would make
	// `annotations: {}` vanish from the diff -- a change the executed record would not carry.
	after := deployment(func(m map[string]any) {
		m["metadata"].(map[string]any)["annotations"] = map[string]any{}
	})
	d, err := Diff(deployment(nil), after)
	if err != nil {
		t.Fatalf("Diff: %v", err)
	}
	if !hasPointer(d, "/metadata/annotations") {
		t.Fatalf("pointers = %v, want /metadata/annotations", pointersOf(t, d))
	}
	assertLeafOps(t, d)
}

func hasPointer(d DiffResult, want string) bool {
	for _, p := range d.Pointers() {
		if p == want {
			return true
		}
	}
	return false
}

// mergeInto deep-merges src into dst, so a fixture can describe only the subtrees it adds.
func mergeInto(dst, src map[string]any) {
	for k, v := range src {
		sm, srcIsMap := v.(map[string]any)
		dm, dstIsMap := dst[k].(map[string]any)
		if srcIsMap && dstIsMap {
			mergeInto(dm, sm)
			continue
		}
		dst[k] = v
	}
}
