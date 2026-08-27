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
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// V-CTR-005 -- envelope schema round-trip; refused keys are ignored or rejected, never honoured
// (06 §4.1, L1). `¬` in 09 §6: the negative control is `verification/mutants/V-CTR-005.json`.
//
// The check has two halves and they need each other.
//
// ROUND-TRIP. An envelope that decodes, re-marshals and decodes again must be the same envelope
// every time. It is the property the idempotency key rests on -- `CompareIdempotencyKey`
// recomputes over the DECODED value and compares against a string the caller hashed over the wire
// bytes, so any field the decoder drops, defaults, coerces or reorders is a key mismatch the caller
// cannot diagnose. It is also the property the journal rests on: the record stores what the broker
// understood, and a reader reconciling a record against the request that produced it is comparing
// exactly these two.
//
// And a round-trip asserted over a corpus proves nothing about a field no fixture carries. Until
// this unit, twelve of the schema's declared paths -- `dryRun`, all four of `cloudTarget`, the
// delete preconditions, `requester.assertion`, `trace.threadId` -- appeared in no valid fixture at
// all, so a decoder that silently dropped any of them would have left the whole corpus green. That
// is why the first test here is a coverage assertion rather than a round-trip: it is the vacuity
// guard, and it is self-maintaining, because adding a field to the wire schema now fails until some
// fixture exercises it.
//
// REFUSED KEYS. "Never honoured" is a claim about a value never reaching a decision, which is
// stronger than "the request was refused". `TestFixtureCorpus` already proves the refusal fires.
// What is proved here is that there is no path by which a reserved key could land even if the scan
// were removed: no field of `Envelope` is spelled with a reserved name, and every reserved name is
// also an unknown field to a bare strict decoder. Two independent mechanisms, and the test fails if
// either one starts carrying the whole weight alone.

// ---- the schema, read out of the type ----

// openPayloads are the two places the schema deliberately stops being closed: they carry arbitrary
// Kubernetes objects and the broker has no business knowing their shape (see
// TestPatchBodyTypeIsNotClosed). Path walking stops here in both directions -- if it did not, a
// `desiredState` that happened to contain a key named `scale` would read as coverage of
// `operations.scale`.
var openPayloads = map[string]bool{
	"operations.desiredState": true,
	"operations.patch.body":   true,
}

// declaredPaths walks the Go type and returns every JSON path the wire schema declares. Slice and
// pointer indirection is transparent: `operations[0].target.name` and `operations[7].target.name`
// are one path, because they are one field.
func declaredPaths(t reflect.Type, prefix string, out map[string]bool) {
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		name, _, _ := strings.Cut(f.Tag.Get("json"), ",")
		if name == "" || name == "-" {
			continue
		}
		path := name
		if prefix != "" {
			path = prefix + "." + name
		}
		out[path] = true
		if openPayloads[path] {
			continue
		}
		ft := f.Type
		for ft.Kind() == reflect.Pointer || ft.Kind() == reflect.Slice {
			ft = ft.Elem()
		}
		if ft.Kind() == reflect.Struct {
			declaredPaths(ft, path, out)
		}
	}
}

// observedPaths walks a decoded JSON document and returns the paths actually present, with array
// indices collapsed so it can be compared to declaredPaths.
func observedPaths(v any, prefix string, out map[string]bool) {
	switch t := v.(type) {
	case map[string]any:
		for k, child := range t {
			path := k
			if prefix != "" {
				path = prefix + "." + k
			}
			out[path] = true
			if openPayloads[path] {
				continue
			}
			observedPaths(child, path, out)
		}
	case []any:
		for _, child := range t {
			observedPaths(child, prefix, out)
		}
	}
}

func TestEveryDeclaredSchemaPathIsExercisedBySomeValidFixture(t *testing.T) {
	declared := map[string]bool{}
	declaredPaths(reflect.TypeOf(Envelope{}), "", declared)
	if len(declared) < 50 {
		t.Fatalf("the walker found only %d declared paths; it has stopped descending", len(declared))
	}

	covered := map[string]bool{}
	for _, path := range fixtures(t, "valid") {
		if filepath.Base(path) == "identities.json" {
			continue
		}
		var doc any
		if err := json.Unmarshal(read(t, path), &doc); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		observedPaths(doc, "", covered)
	}

	var missing []string
	for p := range declared {
		if !covered[p] {
			missing = append(missing, p)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		t.Errorf("%d of %d declared schema paths are carried by no valid fixture, so the round-trip "+
			"below proves nothing about them -- a decoder that dropped one would leave the corpus "+
			"green:\n  %s", len(missing), len(declared), strings.Join(missing, "\n  "))
	}

	// The other direction is deliberately NOT an error. A fixture's `desiredState` is an arbitrary
	// object and its keys are not schema paths. But a top-level key that is not declared would have
	// been refused at decode, and TestFixtureCorpus already decodes every one of these.
}

// ---- the round trip ----

func TestAValidEnvelopeSurvivesTheRoundTrip(t *testing.T) {
	identities := map[string]string{}
	if err := json.Unmarshal(read(t, filepath.Join(fixtureRoot, "valid", "identities.json")), &identities); err != nil {
		t.Fatalf("parse identities.json: %v", err)
	}

	for _, path := range fixtures(t, "valid") {
		name := filepath.Base(path)
		if name == "identities.json" {
			continue
		}
		t.Run(name, func(t *testing.T) {
			raw := read(t, path)

			first, err := DecodeEnvelope(raw)
			if err != nil {
				t.Fatalf("decode: %v", err)
			}

			// 1. Nothing lost, nothing invented, nothing changed. Compared as decoded JSON rather
			// than as bytes: key order and whitespace are not part of the contract, and JCS already
			// owns canonical ordering for the one place it matters.
			remarshalled, err := json.Marshal(first)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			var before, after any
			if err := json.Unmarshal(raw, &before); err != nil {
				t.Fatalf("parse fixture: %v", err)
			}
			if err := json.Unmarshal(remarshalled, &after); err != nil {
				t.Fatalf("parse re-marshalled: %v", err)
			}
			if !reflect.DeepEqual(before, after) {
				lost, gained := diffPaths(before, after)
				t.Errorf("the envelope did not survive decode -> marshal.\n  lost:   %v\n  gained: %v\n  sent: %s\n  back: %s",
					lost, gained, raw, remarshalled)
			}

			// 2. Idempotent as a transform, not just faithful once. A decoder that normalised a
			// field on the way in would pass step 1 the second time round and fail here.
			second, err := DecodeEnvelope(remarshalled)
			if err != nil {
				t.Fatalf("the broker's own output was refused on re-decode: %v", err)
			}
			if !reflect.DeepEqual(first, second) {
				t.Errorf("decode(marshal(decode(x))) != decode(x)\n  first:  %+v\n  second: %+v", first, second)
			}

			// 3. The consequence that is actually load-bearing. The key the caller computed over the
			// wire bytes must still be the key the broker computes over the value it decoded --
			// twice. This is why a dropped field is a caller-visible outage and not a cosmetic bug.
			identity, ok := identities[name]
			if !ok {
				t.Fatalf("no entry in valid/identities.json")
			}
			for i, env := range []*Envelope{first, second} {
				if err := CompareIdempotencyKey(identity, env); err != nil {
					t.Errorf("idempotency key after round-trip %d: %v", i+1, err)
				}
			}
		})
	}
}

// diffPaths reports which paths one document has and the other does not, so a round-trip failure
// names the field instead of printing two envelopes and leaving the reader to spot the difference.
func diffPaths(before, after any) (lost, gained []string) {
	b, a := map[string]bool{}, map[string]bool{}
	observedPaths(before, "", b)
	observedPaths(after, "", a)
	for p := range b {
		if !a[p] {
			lost = append(lost, p)
		}
	}
	for p := range a {
		if !b[p] {
			gained = append(gained, p)
		}
	}
	sort.Strings(lost)
	sort.Strings(gained)
	return lost, gained
}

// ---- never honoured ----

func TestNoReservedKeyHasAPlaceToLand(t *testing.T) {
	// Half one: the type itself. If a future field were spelled `approved`, the reserved-key scan
	// would refuse every envelope carrying the field the broker itself added -- and if the scan were
	// ever reordered after strict decoding, the value would land in the struct. Either way the two
	// lists must be disjoint, and nothing else in the repo says so.
	declared := map[string]bool{}
	declaredPaths(reflect.TypeOf(Envelope{}), "", declared)
	for key := range ReservedKeys {
		if declared[key] {
			t.Errorf("`%s` is both a reserved key and a top-level field of Envelope: it has somewhere to land", key)
		}
	}

	// Half two: defence in depth. Strip the reserved-key scan and the closed schema must still
	// refuse every one of these -- as a plain unknown field, losing the security event but never
	// honouring the value. A reserved name that a bare strict decoder accepts is a name that is
	// refused by exactly one mechanism, and 06 §4.1 spends a paragraph on why that is not enough.
	for key := range ReservedKeys {
		t.Run(key, func(t *testing.T) {
			body := mustJSON(t, withTop(baseEnvelope(), map[string]any{key: "anything at all"}))

			dec := json.NewDecoder(strings.NewReader(string(body)))
			dec.DisallowUnknownFields()
			var into Envelope
			err := dec.Decode(&into)
			if err == nil {
				t.Fatalf("a bare strict decoder accepted `%s`; the reserved-key scan is the only thing refusing it", key)
			}
			if !strings.Contains(err.Error(), "unknown field") {
				t.Fatalf("strict decode failed for the wrong reason: %v", err)
			}

			// And the real path still refuses it loudly, with the evidence.
			_, err = DecodeEnvelope(body)
			var ref *Refusal
			if !errors.As(err, &ref) {
				t.Fatalf("expected a *Refusal, got %v", err)
			}
			if !ref.Journal || !ref.SecurityEvent {
				t.Errorf("`%s` was refused without evidence: journal=%v securityEvent=%v", key, ref.Journal, ref.SecurityEvent)
			}
			wantReason := ReasonReservedKey
			if bypassFamily[key] {
				wantReason = ReasonBypassKey
			}
			if ref.Reason != wantReason {
				t.Errorf("`%s` refused as %q, want %q", key, ref.Reason, wantReason)
			}
		})
	}
}

// TestTheReservedKeyListIsTheOneTheSpecPublishes joins ReservedKeys to 06 §4.1's "What the broker
// ignores -- and what it refuses" table, read as data.
//
// The table is the published contract: it is what an agent author reads, what the tier AGENTS.md
// files point at, and what a reviewer checks a refusal against. `ReservedKeys` is the enforcement.
// Two definition sites of a security rule are only allowed in this repo when something mechanically
// compares them ([[LSN-040]], [[LSN-041]]) -- a key added to the doc and not the map is a promise
// the broker does not keep, and a key added to the map and not the doc refuses callers for a reason
// they cannot look up.
func TestTheReservedKeyListIsTheOneTheSpecPublishes(t *testing.T) {
	const specPath = "../../../docs/designs/broker/spec/06-api-and-data-contracts.md"
	src, err := os.ReadFile(specPath)
	if err != nil {
		t.Fatalf("read %s: %v", specPath, err)
	}
	rows := refusalTable(t, string(src))

	// The table is parsed positionally, so its SHAPE is asserted before its contents. A reordered
	// or extended table must fail loudly here rather than silently redefine which rows are the
	// reserved ones -- that failure mode is the whole risk of reading prose as data.
	if len(rows) != 8 {
		t.Fatalf("the refusal table has %d data rows, expected 8; re-read it before trusting this test:\n%s",
			len(rows), strings.Join(rowClaims(rows), "\n"))
	}
	if !strings.Contains(rows[4].claim, "reused") {
		t.Fatalf("row 5 is %q; the reserved-key block is rows 1-4 and ends where anti-replay begins", rows[4].claim)
	}
	if !strings.Contains(rows[5].claim, "any other unknown field") {
		t.Fatalf("row 6 is %q; the closed-schema catch-all must follow the reserved-key block", rows[5].claim)
	}

	published := map[string]bool{}
	for _, r := range rows[:4] {
		for _, k := range r.keys {
			published[k] = true
		}
	}

	for k := range published {
		if _, ok := ReservedKeys[k]; !ok {
			t.Errorf("06 §4.1 publishes `%s` as refused and ReservedKeys does not carry it: the broker would accept it", k)
		}
	}
	for k := range ReservedKeys {
		if !published[k] {
			t.Errorf("ReservedKeys refuses `%s` and 06 §4.1's table does not mention it: callers are refused for a reason they cannot look up", k)
		}
	}

	// Row 3 is the family the spec singles out as emitting a security event of its own character.
	// A subset assertion, not equality: `approved` and `undoPlan` are in the code's bypassFamily
	// because they have no innocent reading either, which is a widening the table's prose supports
	// ("these names exist only to be rejected loudly") and its row boundaries do not express.
	for _, k := range rows[2].keys {
		if !bypassFamily[k] {
			t.Errorf("06 §4.1 puts `%s` in the loud-refusal row and bypassFamily does not: it would refuse as a plain reserved key", k)
		}
	}
	for k := range bypassFamily {
		if _, ok := ReservedKeys[k]; !ok {
			t.Errorf("`%s` is in bypassFamily and not in ReservedKeys: it changes the reason for a refusal that never fires", k)
		}
	}
}

type refusalRow struct {
	claim string
	keys  []string
}

var backticked = regexp.MustCompile("`([^`]+)`")

// refusalTable extracts the 06 §4.1 table under its own heading. Anchored on the heading rather
// than on a line number, and it takes the contiguous run of table rows that follows -- so a
// paragraph added above or below it is not a test failure and a row added inside it is.
func refusalTable(t *testing.T, src string) []refusalRow {
	t.Helper()
	const heading = "#### What the broker ignores — and what it refuses"
	i := strings.Index(src, heading)
	if i < 0 {
		t.Fatalf("heading %q not found; the table this test reads has moved or been retitled", heading)
	}

	var rows []refusalRow
	started := false
	for _, line := range strings.Split(src[i+len(heading):], "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "|") {
			if started {
				break // the contiguous run of table rows has ended
			}
			continue
		}
		started = true
		cells := strings.Split(strings.Trim(line, "|"), "|")
		if len(cells) != 2 {
			t.Fatalf("table row has %d cells, expected 2: %q", len(cells), line)
		}
		claim := strings.TrimSpace(cells[0])
		if claim == "The envelope claims…" || strings.HasPrefix(claim, "---") {
			continue // header and separator
		}
		var keys []string
		for _, m := range backticked.FindAllStringSubmatch(claim, -1) {
			// `approved: true` publishes the key and the value it would claim; the key is the part
			// the broker matches on.
			key, _, _ := strings.Cut(m[1], ":")
			keys = append(keys, strings.TrimSpace(key))
		}
		rows = append(rows, refusalRow{claim: claim, keys: keys})
	}
	return rows
}

func rowClaims(rows []refusalRow) []string {
	out := make([]string, 0, len(rows))
	for i, r := range rows {
		out = append(out, fmt.Sprintf("  %d. %s", i+1, r.claim))
	}
	return out
}
