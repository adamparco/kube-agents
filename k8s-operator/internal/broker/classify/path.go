package classify

import (
	"fmt"
	"strings"
)

// This file implements the two path dialects of 06 §4.2 and the conversion between them.
//
//	when.fieldPaths (rule authoring)  dotted relaxed JSONPath   spec.template.spec.containers[*].image
//	diff[].path     (output)          RFC 6901 JSON Pointer     /spec/template/spec/containers/0/image
//
// They are never interchangeable, and the spec is explicit that "a reader who assumes one will
// write a rule that never fires". A rule that never fires is the worst possible failure here: it
// is indistinguishable from a rule that fired and found nothing, so a gate that has silently
// stopped gating looks exactly like a gate with nothing to catch. Hence a `fieldPaths` entry
// beginning with `/` is REJECTED at ChangePolicy admission rather than tolerated -- see
// ValidateDottedPath, which the webhook calls.

// pathSegment is one step of a parsed dotted path. Exactly one of Key or Index is meaningful.
type pathSegment struct {
	// Key is a map key. Bracket-quoted segments land here with their quotes removed, which is how
	// metadata.annotations['kube-agents/restarted-at'] survives containing a `/`.
	Key string
	// Index is a list index, or -1 for `[*]` (any element).
	Index int
	// IsIndex distinguishes a list step from a map step, because Index 0 and "no index" are
	// otherwise the same zero value.
	IsIndex bool
}

const anyIndex = -1

// ValidateDottedPath reports why a string is not a valid `when.fieldPaths` entry, or nil.
//
// The leading-slash rejection is a named contract clause, not a nicety: 06 §4.2 specifies the
// message "expected a dotted field path, not a JSON Pointer", and it is specified because the
// mistake is silent. `/spec/replicas` is a perfectly well-formed dotted path with one segment
// literally named "/spec/replicas", which matches nothing and reports nothing.
func ValidateDottedPath(p string) error {
	if p == "" {
		return fmt.Errorf("expected a dotted field path, got an empty string")
	}
	if strings.HasPrefix(p, "/") {
		return fmt.Errorf("expected a dotted field path, not a JSON Pointer")
	}
	if strings.HasPrefix(p, "$") {
		// The strict JSONPath dialect. Same failure shape as the Pointer: it parses as a segment.
		return fmt.Errorf("expected a dotted field path with no leading '$' (the kubectl dialect, e.g. spec.template.spec.containers[*].image)")
	}
	_, err := parseDottedPath(p)
	return err
}

// parseDottedPath splits a dotted relaxed JSONPath into segments.
//
// Grammar, kept deliberately small because every feature added here is a feature a rule author can
// get subtly wrong:
//
//	segment    := bare | bracketed
//	bare       := [^.[]+                      spec, replicas, containers
//	bracketed  := '[' ( int | '*' | quoted ) ']'
//	quoted     := "'" [^']* "'" | '"' [^"]* '"'
//
// A bracket-quoted segment is a MAP KEY, not an index: `metadata.annotations['a.b/c']` is three
// segments, the last of which contains both a `.` and a `/`. That is the escape hatch 06 §4.2
// documents, and without it no rule could ever name a Kubernetes annotation.
func parseDottedPath(p string) ([]pathSegment, error) {
	var segs []pathSegment
	i := 0
	for i < len(p) {
		switch p[i] {
		case '.':
			// A leading, doubled or trailing dot yields an empty segment. Rejected rather than
			// skipped: `spec..replicas` is a typo, and silently reading it as `spec.replicas`
			// means the author never learns the rule they wrote is not the rule they meant.
			if i == 0 || i == len(p)-1 || p[i+1] == '.' {
				return nil, fmt.Errorf("empty path segment at offset %d in %q", i, p)
			}
			i++
		case '[':
			end := strings.IndexByte(p[i:], ']')
			if end < 0 {
				return nil, fmt.Errorf("unterminated '[' at offset %d in %q", i, p)
			}
			inner := p[i+1 : i+end]
			seg, err := parseBracket(inner, p)
			if err != nil {
				return nil, err
			}
			segs = append(segs, seg)
			i += end + 1
		default:
			j := i
			for j < len(p) && p[j] != '.' && p[j] != '[' {
				j++
			}
			segs = append(segs, pathSegment{Key: p[i:j]})
			i = j
		}
	}
	if len(segs) == 0 {
		return nil, fmt.Errorf("empty path %q", p)
	}
	return segs, nil
}

func parseBracket(inner, whole string) (pathSegment, error) {
	if inner == "*" {
		return pathSegment{Index: anyIndex, IsIndex: true}, nil
	}
	if len(inner) >= 2 && (inner[0] == '\'' || inner[0] == '"') && inner[len(inner)-1] == inner[0] {
		key := inner[1 : len(inner)-1]
		if key == "" {
			return pathSegment{}, fmt.Errorf("empty quoted segment in %q", whole)
		}
		return pathSegment{Key: key}, nil
	}
	n := 0
	if inner == "" {
		return pathSegment{}, fmt.Errorf("empty '[]' in %q", whole)
	}
	for _, r := range inner {
		if r < '0' || r > '9' {
			return pathSegment{}, fmt.Errorf("bracket segment %q in %q is neither an index, '*', nor a quoted key", inner, whole)
		}
		n = n*10 + int(r-'0')
	}
	return pathSegment{Index: n, IsIndex: true}, nil
}

// escapePointerToken applies RFC 6901 §3: `~` becomes `~0`, `/` becomes `~1`. Order matters and is
// the classic trap -- escaping `/` first would turn `a/b` into `a~1b` and then into `a~01b`.
func escapePointerToken(s string) string {
	s = strings.ReplaceAll(s, "~", "~0")
	return strings.ReplaceAll(s, "/", "~1")
}

// unescapePointerToken is the inverse, and its order is the mirror trap: `~1` must be undone before
// `~0`, or `~01` (an escaped `~1`, i.e. a literal "~1") decodes to `/`.
func unescapePointerToken(s string) string {
	s = strings.ReplaceAll(s, "~1", "/")
	return strings.ReplaceAll(s, "~0", "~")
}

// splitPointer splits an RFC 6901 pointer into unescaped tokens. The empty pointer "" is the whole
// document and yields no tokens; "/" is one empty token, which is a legal (if strange) key.
func splitPointer(ptr string) []string {
	if ptr == "" {
		return nil
	}
	parts := strings.Split(strings.TrimPrefix(ptr, "/"), "/")
	out := make([]string, len(parts))
	for i, p := range parts {
		out[i] = unescapePointerToken(p)
	}
	return out
}

// PointerPrefixMatch reports whether a dotted rule path matches a JSON Pointer from a diff, by
// PREFIX CONTAINMENT: `spec.template` matches a change at `/spec/template/spec/containers/0/image`.
//
// Prefix containment, not equality, is what makes rules writable. A rule that had to name the exact
// leaf would need one entry per field a Deployment has; `when.fieldPaths: [spec.template]` says
// "any change to the pod template" once. The direction is fixed and is not symmetric: the RULE is
// the prefix and the DIFF is the longer path. A diff at `/spec` does not match a rule for
// `spec.template.spec.containers[*].image` -- a change to the whole of `spec` genuinely does touch
// the image, but it also touches everything else, and the rules that care about wholesale replacement
// say so by naming `spec`.
func PointerPrefixMatch(dotted string, pointer string) (bool, error) {
	segs, err := parseDottedPath(dotted)
	if err != nil {
		return false, err
	}
	tokens := splitPointer(pointer)
	if len(segs) > len(tokens) {
		return false, nil
	}
	for i, seg := range segs {
		tok := tokens[i]
		if seg.IsIndex {
			if seg.Index == anyIndex {
				// `[*]` matches any single token. It does NOT verify the token is numeric: a
				// strategic-merge diff can address a list element by key rather than by position
				// (`/spec/containers/app/image`), and a rule written with `[*]` means "any element"
				// in both dialects.
				continue
			}
			if tok != fmt.Sprint(seg.Index) {
				return false, nil
			}
			continue
		}
		if tok != seg.Key {
			return false, nil
		}
	}
	return true, nil
}

// TouchedPaths returns the union of `path` and `from` across an RFC 6902 JSON Patch, which is the
// set 06 §4.2 says fieldPaths is matched against: "so a `remove` counts as touching the path it
// removed", and a `move` counts as touching both ends.
func TouchedPaths(patch []PatchOp) []string {
	seen := make(map[string]bool, len(patch)*2)
	var out []string
	add := func(p string) {
		if p == "" || seen[p] {
			return
		}
		seen[p] = true
		out = append(out, p)
	}
	for _, op := range patch {
		add(op.Path)
		add(op.From)
	}
	return out
}

// PatchOp is one RFC 6902 operation. Only the fields the classifier reads are modelled -- Value is
// `any` and is used by the secret-material scan, not by path matching.
type PatchOp struct {
	Op    string `json:"op"`
	Path  string `json:"path"`
	From  string `json:"from,omitempty"`
	Value any    `json:"value,omitempty"`
}

// DottedToPointerPrefix renders a dotted path as the Pointer prefix it corresponds to, for use in
// messages and fixtures. `[*]` renders as `/*`, which is NOT a valid Pointer and is deliberately
// not passed to anything that parses one -- it exists so a reason string can show the author what
// their rule matched on.
func DottedToPointerPrefix(dotted string) (string, error) {
	segs, err := parseDottedPath(dotted)
	if err != nil {
		return "", err
	}
	var b strings.Builder
	for _, seg := range segs {
		b.WriteByte('/')
		switch {
		case seg.IsIndex && seg.Index == anyIndex:
			b.WriteByte('*')
		case seg.IsIndex:
			fmt.Fprintf(&b, "%d", seg.Index)
		default:
			b.WriteString(escapePointerToken(seg.Key))
		}
	}
	return b.String(), nil
}
