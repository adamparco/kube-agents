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

package broker

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode/utf16"
)

// RFC 8785 JSON Canonicalization Scheme.
//
// 06 §4.1 specifies JCS by name and adds "Not `json.Marshal` with sorted keys, which differs on
// numbers and escapes." That warning is the whole reason this file exists. Go's encoder and JCS
// disagree in three places, and each disagreement is a silent duplicate action rather than an
// error, because a caller that computes the key one way and a broker that recomputes it the other
// way will simply never match:
//
//  1. NUMBERS. `json.Marshal(1e21)` emits `1e+21`; JCS emits `1e+21` too, but `json.Marshal` of a
//     float64 100 emits `100` while a `json.Number` "1.0e2" round-trips as the literal text. JCS
//     always converts through IEEE-754 double and re-serialises with the ECMAScript
//     Number::toString algorithm, so `100`, `1.0e2` and `1e2` all canonicalise to `100`.
//  2. KEY ORDER. Go sorts map keys as UTF-8 byte strings. JCS sorts them as UTF-16 code-unit
//     sequences. Those orders differ for any key mixing a BMP character above U+E000 with a
//     supplementary one -- an emoji in a label value is enough to split them.
//  3. ESCAPES. Go's encoder HTML-escapes `<`, `>` and `&` into `<` and friends by default.
//     JCS escapes only what JSON requires. A `desiredState` containing an HTML fragment or a shell
//     redirect would canonicalise differently under the two.
//
// Written in-tree rather than pulled from a module deliberately. V-RUN-010 asserts the broker's
// SBOM stays minimal, and this is ~150 lines against a frozen RFC with published test vectors --
// a dependency's worth of supply-chain surface for a file that will never need to change.

// Canonicalize returns the RFC 8785 canonical form of a JSON document.
//
// It takes and returns bytes rather than a Go value so it can be tested directly against the RFC's
// published vectors: a canonicaliser that only accepts already-decoded values cannot be shown to
// handle the number literals that are most of what the vectors exercise.
func Canonicalize(input []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(input))
	// Without UseNumber every literal becomes a float64 and the distinction the RFC cares about --
	// what the source text said versus what the double holds -- is gone before we can act on it.
	dec.UseNumber()

	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("jcs: parse: %w", err)
	}
	if dec.More() {
		return nil, fmt.Errorf("jcs: input contains more than one JSON value")
	}

	var buf bytes.Buffer
	if err := writeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCanonical(buf *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case json.Number:
		s, err := esNumber(t)
		if err != nil {
			return err
		}
		buf.WriteString(s)
	case string:
		writeCanonicalString(buf, t)
	case []any:
		buf.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sortUTF16(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeCanonicalString(buf, k)
			buf.WriteByte(':')
			if err := writeCanonical(buf, t[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		// Reachable only if a caller hands writeCanonical a Go value that did not come out of the
		// decoder above. Refusing beats guessing: a canonicaliser that silently coerces an unknown
		// type produces a key that is stable within one binary and different in the next.
		return fmt.Errorf("jcs: unsupported value of type %T", v)
	}
	return nil
}

// sortUTF16 orders strings by UTF-16 code unit, which is what RFC 8785 §3.2.3 requires.
//
// Deliberately not `sort.Strings`. Go compares UTF-8 bytes, and for code points in [U+E000,U+FFFF]
// versus supplementary planes the two orders are opposite: U+FFFD encodes as EF BF BD in UTF-8 but
// as the single unit 0xFFFD in UTF-16, while U+1F600 encodes as F0 9F 98 80 (greater in UTF-8) but
// as the surrogate pair D83D DE00 (lesser in UTF-16). Two keys straddling that boundary sort one
// way here and the other way in every JCS implementation written in a UTF-16 language.
func sortUTF16(keys []string) {
	encoded := make(map[string][]uint16, len(keys))
	for _, k := range keys {
		encoded[k] = utf16.Encode([]rune(k))
	}
	// Insertion sort: key sets here are object field lists, which are small, and this keeps the
	// comparison inline and obvious rather than hidden behind a closure.
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && lessUTF16(encoded[keys[j]], encoded[keys[j-1]]); j-- {
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
}

func lessUTF16(a, b []uint16) bool {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return len(a) < len(b)
}

const hexDigits = "0123456789abcdef"

// stringSink is what writeCanonicalString writes into. An interface rather than *bytes.Buffer so
// tests can render one string without standing up the whole encoder -- and so the assertion in
// jcs_test.go uses the SHIPPED escaper rather than a re-implementation of it, which is the only
// version of that assertion worth making.
type stringSink interface {
	WriteByte(byte) error
	WriteString(string) (int, error)
	WriteRune(rune) (int, error)
}

// writeCanonicalString applies RFC 8785 §3.2.2.2: escape exactly what JSON requires and nothing
// else. Note the two things this does NOT do, both of which Go's encoder does by default:
// HTML-escape `<>&`, and escape U+2028/U+2029. Adding either would produce output no other JCS
// implementation agrees with.
func writeCanonicalString(buf stringSink, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		default:
			if r < 0x20 {
				buf.WriteString(`\u00`)
				buf.WriteByte(hexDigits[(r>>4)&0xF])
				buf.WriteByte(hexDigits[r&0xF])
				continue
			}
			buf.WriteRune(r)
		}
	}
	buf.WriteByte('"')
}

// esNumber implements ECMAScript Number::toString(x) for base 10, which RFC 8785 §3.2.2.3 adopts
// by reference. The shape of the algorithm is: find the shortest digit string s and exponent n
// such that s x 10^(n-k) round-trips to x, then choose one of five renderings by where n falls.
//
// Go gives us s and n almost for free -- `strconv.FormatFloat(f, 'e', -1, 64)` is defined to
// produce the shortest representation that parses back exactly, which is the same "k as small as
// possible" the spec asks for. What Go does not give us is the rendering rules, which are the part
// that differs: Go's 'g' verb switches to exponential at a different threshold than ECMAScript
// does, so `strconv.FormatFloat(f, 'g', -1, 64)` would emit `1e-05` where JavaScript emits
// `0.00001`, and the two keys would never match.
func esNumber(n json.Number) (string, error) {
	f, err := n.Float64()
	if err != nil {
		return "", fmt.Errorf("jcs: %q is not representable as a double: %w", n.String(), err)
	}
	if math.IsNaN(f) || math.IsInf(f, 0) {
		// Unreachable through encoding/json, which rejects these literals, but reachable if a
		// caller builds json.Number by hand. JSON has no spelling for either.
		return "", fmt.Errorf("jcs: %q is not a finite number", n.String())
	}
	if f == 0 {
		// Covers -0 as well: ECMAScript renders negative zero as "0", so the canonical form of
		// `-0` and `0` is the same text. Without this branch the sign handling below would emit
		// "-0" and split two documents that JCS considers identical.
		return "0", nil
	}

	neg := f < 0
	if neg {
		f = -f
	}

	mantissa, expText, _ := strings.Cut(strconv.FormatFloat(f, 'e', -1, 64), "e")
	exp, err := strconv.Atoi(expText)
	if err != nil {
		return "", fmt.Errorf("jcs: cannot read exponent of %v: %w", f, err)
	}
	digits := strings.Replace(mantissa, ".", "", 1)
	k := len(digits)
	// The spec's n: the position of the decimal point relative to the digit string.
	pos := exp + 1

	var out string
	switch {
	case k <= pos && pos <= 21:
		out = digits + strings.Repeat("0", pos-k)
	case 0 < pos && pos <= 21:
		out = digits[:pos] + "." + digits[pos:]
	case -6 < pos && pos <= 0:
		out = "0." + strings.Repeat("0", -pos) + digits
	default:
		e := pos - 1
		sign := "+"
		if e < 0 {
			sign = "-"
			e = -e
		}
		if k == 1 {
			out = digits + "e" + sign + strconv.Itoa(e)
		} else {
			out = digits[:1] + "." + digits[1:] + "e" + sign + strconv.Itoa(e)
		}
	}
	if neg {
		out = "-" + out
	}
	return out, nil
}
