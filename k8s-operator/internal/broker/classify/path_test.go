package classify

import (
	"strings"
	"testing"
)

func TestValidateDottedPath(t *testing.T) {
	cases := []struct {
		name    string
		path    string
		wantErr string
	}{
		{"simple", "spec.replicas", ""},
		{"wildcard index", "spec.template.spec.containers[*].image", ""},
		{"literal index", "spec.containers[0].image", ""},
		{"quoted key with a slash", "metadata.annotations['kube-agents/restarted-at']", ""},
		{"quoted key with a dot", `metadata.labels["app.kubernetes.io/name"]`, ""},
		{"single segment", "rules", ""},

		// The named contract clause of 06 §4.2. The message is asserted verbatim because the spec
		// specifies it: a rule author who pasted a Pointer gets told exactly what is wrong.
		{"json pointer", "/spec/replicas", "expected a dotted field path, not a JSON Pointer"},
		{"json pointer, root", "/", "expected a dotted field path, not a JSON Pointer"},
		{"strict jsonpath", "$.spec.replicas", "leading '$'"},

		{"empty", "", "empty string"},
		{"trailing dot", "spec.", "empty path segment"},
		{"leading dot", ".spec", "empty path segment"},
		{"doubled dot", "spec..replicas", "empty path segment"},
		{"unterminated bracket", "spec.containers[0", "unterminated '['"},
		{"empty bracket", "spec.containers[]", "empty '[]'"},
		{"garbage bracket", "spec.containers[abc]", "neither an index"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateDottedPath(tc.path)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("ValidateDottedPath(%q) = %v, want nil", tc.path, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("ValidateDottedPath(%q) = nil, want an error containing %q", tc.path, tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("ValidateDottedPath(%q) = %q, want it to contain %q", tc.path, err, tc.wantErr)
			}
		})
	}
}

// TestPointerEscapeOrdering pins both halves of the RFC 6901 §3 ordering trap.
//
// Escaping `/` before `~` turns `a/b` into `a~1b` and then into `a~01b`. Unescaping `~0` before
// `~1` turns the literal string `~1` into `/`. Both are one transposed line away and neither
// produces a compile error or an obviously wrong-looking result -- they produce a pointer that
// addresses a field nobody has.
func TestPointerEscapeOrdering(t *testing.T) {
	cases := []struct {
		raw     string
		escaped string
	}{
		{"plain", "plain"},
		{"a/b", "a~1b"},
		{"a~b", "a~0b"},
		{"kube-agents/environment", "kube-agents~1environment"},

		// The two that catch a transposed ReplaceAll.
		{"a~1b", "a~01b"},
		{"~/", "~0~1"},
		{"/~", "~1~0"},
	}
	for _, tc := range cases {
		t.Run(tc.raw, func(t *testing.T) {
			if got := escapePointerToken(tc.raw); got != tc.escaped {
				t.Fatalf("escapePointerToken(%q) = %q, want %q", tc.raw, got, tc.escaped)
			}
			if got := unescapePointerToken(tc.escaped); got != tc.raw {
				t.Fatalf("unescapePointerToken(%q) = %q, want %q (round trip)", tc.escaped, got, tc.raw)
			}
		})
	}
}

func TestSplitPointer(t *testing.T) {
	cases := []struct {
		ptr  string
		want []string
	}{
		{"", nil},
		{"/spec", []string{"spec"}},
		{"/spec/replicas", []string{"spec", "replicas"}},
		{"/spec/template/spec/containers/0/image", []string{"spec", "template", "spec", "containers", "0", "image"}},
		{"/metadata/annotations/kube-agents~1restarted-at", []string{"metadata", "annotations", "kube-agents/restarted-at"}},
		// "/" is one EMPTY token, which is a legal JSON key. Reading it as zero tokens would make
		// the root pointer and the empty-key pointer the same thing.
		{"/", []string{""}},
	}
	for _, tc := range cases {
		t.Run(tc.ptr, func(t *testing.T) {
			got := splitPointer(tc.ptr)
			if len(got) != len(tc.want) {
				t.Fatalf("splitPointer(%q) = %#v, want %#v", tc.ptr, got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("splitPointer(%q)[%d] = %q, want %q", tc.ptr, i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestPointerPrefixMatch(t *testing.T) {
	cases := []struct {
		name    string
		dotted  string
		pointer string
		want    bool
	}{
		{"exact", "spec.replicas", "/spec/replicas", true},
		{"rule is a prefix of the diff", "spec.template", "/spec/template/spec/containers/0/image", true},
		{"wildcard matches an index", "spec.template.spec.containers[*].image", "/spec/template/spec/containers/0/image", true},
		{"wildcard matches a later index", "spec.template.spec.containers[*].image", "/spec/template/spec/containers/7/image", true},
		{"literal index matches itself", "spec.containers[0].image", "/spec/containers/0/image", true},
		{"literal index does not match another", "spec.containers[0].image", "/spec/containers/1/image", false},
		{"quoted key with a slash", "metadata.annotations['kube-agents/restarted-at']", "/metadata/annotations/kube-agents~1restarted-at", true},

		// THE DIRECTION. The rule is the prefix; the diff is the longer path. A diff at /spec does
		// not match a rule naming a leaf under spec -- see the comment on PointerPrefixMatch for
		// why that is deliberate and not a missing case.
		{"diff shorter than rule does not match", "spec.template.spec.containers[*].image", "/spec", false},
		{"sibling does not match", "spec.replicas", "/spec/paused", false},

		// A wildcard matching a NON-numeric token, which strategic-merge key addressing produces.
		{"wildcard matches a named list element", "spec.containers[*].image", "/spec/containers/app/image", true},

		// A prefix of a token is not a match: `spec.rep` must not match `/spec/replicas`.
		{"token prefix is not a match", "spec.rep", "/spec/replicas", false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := PointerPrefixMatch(tc.dotted, tc.pointer)
			if err != nil {
				t.Fatalf("PointerPrefixMatch(%q, %q) errored: %v", tc.dotted, tc.pointer, err)
			}
			if got != tc.want {
				t.Fatalf("PointerPrefixMatch(%q, %q) = %v, want %v", tc.dotted, tc.pointer, got, tc.want)
			}
		})
	}
}

// TestPointerPrefixMatchRejectsPointerAsRule is the failure mode the whole dialect split exists to
// prevent, asserted end to end: a Pointer used where a dotted path belongs matches NOTHING, silently,
// which is why ValidateDottedPath rejects it at admission instead of letting it through to here.
func TestPointerPrefixMatchRejectsPointerAsRule(t *testing.T) {
	got, err := PointerPrefixMatch("/spec/replicas", "/spec/replicas")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got {
		t.Fatal("a JSON Pointer used as a dotted rule path matched; if this now works, " +
			"the leading-slash rejection in ValidateDottedPath is no longer load-bearing and 06 §4.2 needs revisiting")
	}
}

func TestTouchedPaths(t *testing.T) {
	patch := []PatchOp{
		{Op: "replace", Path: "/spec/replicas", Value: 3},
		{Op: "remove", Path: "/spec/template/spec/securityContext"},
		{Op: "move", From: "/spec/a", Path: "/spec/b"},
		{Op: "replace", Path: "/spec/replicas", Value: 4}, // duplicate
	}
	got := TouchedPaths(patch)
	want := []string{"/spec/replicas", "/spec/template/spec/securityContext", "/spec/b", "/spec/a"}
	if len(got) != len(want) {
		t.Fatalf("TouchedPaths = %#v, want %#v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("TouchedPaths[%d] = %q, want %q (order must be stable)", i, got[i], want[i])
		}
	}
}

// TestTouchedPathsIncludesRemoveAndMove is the specific clause of 06 §4.2 -- "so a `remove` counts
// as touching the path it removed". A patch that only removes a field must still fire a fieldPaths
// rule naming that field, which is the entire mechanism behind `security-loosen`.
func TestTouchedPathsIncludesRemoveAndMove(t *testing.T) {
	touched := TouchedPaths([]PatchOp{{Op: "remove", Path: "/spec/template/spec/securityContext/runAsNonRoot"}})
	ok, err := PointerPrefixMatch("spec.template.spec.securityContext", touched[0])
	if err != nil || !ok {
		t.Fatalf("a removal of a securityContext field must match a securityContext rule; got (%v, %v)", ok, err)
	}
}

func TestDottedToPointerPrefix(t *testing.T) {
	cases := []struct{ dotted, want string }{
		{"spec.replicas", "/spec/replicas"},
		{"spec.containers[0].image", "/spec/containers/0/image"},
		{"spec.containers[*].image", "/spec/containers/*/image"},
		{"metadata.annotations['kube-agents/restarted-at']", "/metadata/annotations/kube-agents~1restarted-at"},
	}
	for _, tc := range cases {
		t.Run(tc.dotted, func(t *testing.T) {
			got, err := DottedToPointerPrefix(tc.dotted)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Fatalf("DottedToPointerPrefix(%q) = %q, want %q", tc.dotted, got, tc.want)
			}
		})
	}
}
