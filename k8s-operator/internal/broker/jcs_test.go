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
	"encoding/json"
	"strings"
	"testing"
)

// Every case in this file spells control characters and astral code points with \u escapes rather
// than embedding the bytes. Not cosmetic: a raw U+0001 or U+2028 in Go source is invisible in a
// diff, survives a copy-paste as something else, and in the case of NUL is not legal Go at all --
// so a test about escaping would fail to compile for a reason unrelated to escaping.

// TestJCSNumbers exercises the ECMAScript Number::toString renderings RFC 8785 §3.2.2.3 adopts.
//
// The cases are chosen at the boundaries where a plausible-looking implementation diverges, not
// for coverage. Every one of these is a value Go's own `strconv.FormatFloat(f, 'g', -1, 64)` --
// the obvious shortcut -- formats differently from ECMAScript.
func TestJCSNumbers(t *testing.T) {
	cases := []struct{ in, want string }{
		// Integers stay integers. `1.0` and `1e0` are the same double as `1`, so they canonicalise
		// identically -- this is the property that makes a key computed by a JavaScript client
		// match one computed here.
		{"1", "1"},
		{"1.0", "1"},
		{"1e0", "1"},
		{"100", "100"},
		{"1.0e2", "100"},

		// Zero, including the negative one. ECMAScript renders -0 as "0"; an implementation that
		// carried the sign would split two documents JCS considers identical.
		{"0", "0"},
		{"-0", "0"},
		{"0.0", "0"},

		// The decimal/exponential switch points. ECMAScript uses decimal notation for
		// 1e-6 <= |x| < 1e21 and exponential outside it; Go's 'g' verb switches at a different
		// place, which is the single most likely way to get this wrong.
		{"0.000001", "0.000001"},
		{"1e-6", "0.000001"},
		{"1e-7", "1e-7"},
		{"0.0000001", "1e-7"},
		{"1e20", "100000000000000000000"},
		{"1e21", "1e+21"},
		{"1e22", "1e+22"},

		// Sign of the exponent is always present, and the exponent itself is unpadded: "1e-7",
		// never "1e-07".
		{"1e30", "1e+30"},
		{"1.5e-10", "1.5e-10"},
		{"-1.5e-10", "-1.5e-10"},

		// Shortest round-trip. 0.1 has no exact double, and an implementation printing full
		// precision would emit 0.1000000000000000055511151231257827.
		{"0.1", "0.1"},
		{"1.5", "1.5"},
		{"333333333.33333329", "333333333.3333333"},

		// The extremes of the double range, where the digit-string logic is most exposed.
		{"9007199254740992", "9007199254740992"},
		{"5e-324", "5e-324"},
		{"1.7976931348623157e308", "1.7976931348623157e+308"},
		{"2.2250738585072014e-308", "2.2250738585072014e-308"},

		// Negatives take the sign after the rendering, not before.
		{"-1", "-1"},
		{"-100", "-100"},
		{"-0.000001", "-0.000001"},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			got, err := Canonicalize([]byte(c.in))
			if err != nil {
				t.Fatalf("Canonicalize(%s): %v", c.in, err)
			}
			if string(got) != c.want {
				t.Fatalf("Canonicalize(%s) = %s, want %s", c.in, got, c.want)
			}
		})
	}
}

// TestJCSKeyOrderIsUTF16 is the RFC 8785 §3.2.3 ordering, and the reason sortUTF16 exists.
//
// The input is the RFC's own Appendix B example. The load-bearing pair is U+1F602 and U+FB33: in
// UTF-16 the emoji sorts FIRST, because its leading surrogate D83D is below FB33. In UTF-8 --
// which `sort.Strings` uses -- it sorts LAST, because F0 is above EF. An implementation that
// reached for sort.Strings passes every other test in this file and produces keys no JavaScript
// client can reproduce.
func TestJCSKeyOrderIsUTF16(t *testing.T) {
	input := "{" +
		`"\u20ac":"Euro Sign",` +
		`"\r":"Carriage Return",` +
		`"\n":"Newline",` +
		`"1":"One",` +
		`"\u0080":"Control",` +
		`"\ud83d\ude02":"Smiley",` +
		`"\u00f6":"Latin Small Letter O With Diaeresis",` +
		`"\ufb33":"Hebrew Letter Dalet With Dagesh",` +
		`"</script>":"Browser Challenge"` +
		"}"

	got, err := Canonicalize([]byte(input))
	if err != nil {
		t.Fatalf("Canonicalize: %v", err)
	}
	if !json.Valid(got) {
		t.Fatalf("canonical output is not valid JSON:\n%s", got)
	}

	// The RFC's expected order: \n < \r < "1" < "</script>" < U+0080 < U+00F6 < U+20AC < U+1F602
	// < U+FB33.
	want := []string{"\n", "\r", "1", "</script>", "\u0080", "\u00f6", "\u20ac", "\U0001F602", "\ufb33"}
	last := -1
	for i, k := range want {
		// Located with the SHIPPED escaper, so this asserts ORDER without also asserting escaping.
		// Escaping has its own test below, and a single test that fails for either reason tells
		// you neither.
		spelled := canonicalStringOf(k) + ":"
		idx := strings.Index(string(got), spelled)
		if idx < 0 {
			t.Fatalf("key %d (%s) missing from output:\n%s", i, spelled, got)
		}
		if idx <= last {
			t.Fatalf("key %d (%s) is at %d, at or before the previous key's %d\noutput: %s", i, spelled, idx, last, got)
		}
		last = idx
	}

	// The load-bearing pair, asserted on its own so a failure names the property that broke rather
	// than just "key 8 is in the wrong place".
	emoji := strings.Index(string(got), canonicalStringOf("\U0001F602"))
	dalet := strings.Index(string(got), canonicalStringOf("\ufb33"))
	if emoji > dalet {
		t.Fatalf("U+1F602 sorted after U+FB33: keys are being ordered by UTF-8 bytes, not UTF-16 code units")
	}
}

// TestJCSEscapes covers RFC 8785 §3.2.2.2, and in particular the two things Go's encoder does that
// JCS forbids.
func TestJCSEscapes(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"quote and backslash", `"a\"b\\c"`, `"a\"b\\c"`},
		{"the five short escapes", "\"\\b\\t\\n\\f\\r\"", `"\b\t\n\f\r"`},
		// Every other C0 control is \u00xx in LOWERCASE hex. RFC 8785 §3.2.2.2 fixes the case, and
		// \u001F versus \u001f is a different byte string and so a different key.
		{"other controls are \\u00xx lowercase", "\"\\u0001\\u001F\"", `"\u0001\u001f"`},
		// Go's json.Marshal emits \u003c for `<`. JCS does not escape it, so a broker using
		// json.Marshal would compute a different key for any payload containing HTML or a shell
		// redirect -- and `<` appears in perfectly ordinary container args.
		{"no HTML escaping", `"</script>&<>"`, `"</script>&<>"`},
		// Go's encoder escapes U+2028/U+2029 for JavaScript-embedding safety. JCS does not: they
		// are ordinary characters above U+001F and pass through as themselves.
		{"no line-separator escaping", "\"a\\u2028b\\u2029c\"", "\"a\u2028b\u2029c\""},
		{"non-ASCII passes through literally", "\"\\u00f6\\u20ac\"", "\"\u00f6\u20ac\""},
		// U+007F is not a JSON control character. Go's encoder leaves it alone too, but an
		// implementation that escaped "everything unprintable" would not.
		{"DEL is not a control for JSON purposes", "\"\\u007f\"", "\"\u007f\""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := Canonicalize([]byte(c.in))
			if err != nil {
				t.Fatalf("Canonicalize: %v", err)
			}
			if string(got) != c.want {
				t.Fatalf("got %q, want %q", got, c.want)
			}
		})
	}
}

// TestJCSDivergesFromJSONMarshal is the claim in 06 §4.1 stated as a test: "Not `json.Marshal`
// with sorted keys, which differs on numbers and escapes."
//
// It is worth being precise about WHERE, because most of the number surface is not a divergence at
// all: Go's float64 encoder deliberately implements the same shortest-round-trip and the same
// 1e-6/1e21 decimal-versus-exponential switch that ECMAScript does, so `1e21`, `1e-7`, `5e-324`
// and `1e20` all come out identical. The three cases below are the whole of it, and each is enough
// on its own to make a caller's key and the broker's recomputation disagree forever.
//
// If a case here ever fails it does not mean the canonicaliser is broken -- it means Go's encoder
// changed, and the reason this file exists needs re-reading before anyone deletes it.
func TestJCSDivergesFromJSONMarshal(t *testing.T) {
	cases := []struct{ name, in string }{
		// Go HTML-escapes `<`, `>` and `&` by default. JCS escapes only what JSON requires.
		{"HTML escaping", `{"a":"<b>&</b>"}`},
		// Negative zero. Go preserves the sign; ECMAScript's Number::toString, which JCS adopts,
		// renders it "0". A `replicas: -0` is absurd, but a computed field that underflowed to -0
		// is not, and it would split two envelopes JCS considers identical.
		{"negative zero", `{"a":-0}`},
		// Key order: Go sorts UTF-8 bytes, JCS sorts UTF-16 code units. U+FB33 and U+1F602 are on
		// opposite sides of that difference. This is the one that needs no exotic value at all --
		// an emoji in a label is enough.
		{"UTF-16 key order", "{\"דּ\":1,\"\U0001F602\":2}"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var v any
			if err := json.Unmarshal([]byte(c.in), &v); err != nil {
				t.Fatalf("unmarshal %s: %v", c.in, err)
			}
			marshalled, err := json.Marshal(v)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			canonical, err := Canonicalize([]byte(c.in))
			if err != nil {
				t.Fatalf("canonicalize: %v", err)
			}
			if string(marshalled) == string(canonical) {
				t.Errorf("json.Marshal and JCS agree on %s (both %s); the divergence this case exists for may have moved", c.in, canonical)
			}
		})
	}
}

// TestJCSIsIdempotent. Canonicalising canonical output must be a no-op, or the form is not
// canonical -- and a broker that canonicalised twice somewhere in a retry would get a different
// key the second time.
func TestJCSIsIdempotent(t *testing.T) {
	inputs := []string{
		`{"b":1,"a":[1,2,{"z":null,"y":true}],"c":"\u00f6"}`,
		`[1e21,1e-7,0.1,-0,"</script>"]`,
		`{"nested":{"deeply":{"ordered":{"z":1,"a":2}}}}`,
	}
	for _, in := range inputs {
		once, err := Canonicalize([]byte(in))
		if err != nil {
			t.Fatalf("first pass on %s: %v", in, err)
		}
		twice, err := Canonicalize(once)
		if err != nil {
			t.Fatalf("second pass on %s: %v", once, err)
		}
		if string(once) != string(twice) {
			t.Fatalf("not idempotent:\n  once:  %s\n  twice: %s", once, twice)
		}
	}
}

// TestJCSStructuralCases covers the shapes that carry no numbers or strings, where an
// implementation can still get separators or empties wrong.
func TestJCSStructuralCases(t *testing.T) {
	cases := []struct{ in, want string }{
		{`{}`, `{}`},
		{`[]`, `[]`},
		{`null`, `null`},
		{`true`, `true`},
		{`[null,true,false]`, `[null,true,false]`},
		{`{ "a" : { } , "b" : [ ] }`, `{"a":{},"b":[]}`},
		// Array order is DATA, not something to sort. An implementation that sorted arrays as well
		// as objects would silently change the meaning of a JSON Patch.
		{`[3,1,2]`, `[3,1,2]`},
		{`{"b":1,"a":2}`, `{"a":2,"b":1}`},
		// Lexicographic, not natural: "a10" sorts before "a2" because '1' < '2'.
		{`{"a10":1,"a2":2}`, `{"a10":1,"a2":2}`},
	}
	for _, c := range cases {
		t.Run(c.in, func(t *testing.T) {
			got, err := Canonicalize([]byte(c.in))
			if err != nil {
				t.Fatalf("Canonicalize: %v", err)
			}
			if string(got) != c.want {
				t.Fatalf("got %s, want %s", got, c.want)
			}
		})
	}
}

// TestJCSRefusesJunk. A canonicaliser that returns something for a non-document produces a key,
// and a key computed over garbage is a key that collides with another piece of garbage.
func TestJCSRefusesJunk(t *testing.T) {
	for _, in := range []string{``, `{`, `{"a":1}{"b":2}`, `nope`, `{"a":1} trailing`} {
		if out, err := Canonicalize([]byte(in)); err == nil {
			t.Errorf("Canonicalize(%q) returned %s; it must refuse", in, out)
		}
	}
}

// canonicalStringOf renders s with the shipped escaper, for use in assertions.
func canonicalStringOf(s string) string {
	var b strings.Builder
	writeCanonicalString(&b, s)
	return b.String()
}
