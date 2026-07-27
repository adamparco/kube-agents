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
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fixtureRoot is the 09 §7.2 corpus. Reached by relative path rather than copied into testdata so
// there is exactly one set of fixtures: the L2 conformance run reads the same files, and two
// copies would drift the first time somebody fixed one of them.
const fixtureRoot = "../../../verification/fixtures/envelopes"

// TestFixtureCorpus is V-CTR-005 and the refusal half of V-BRK-002.
//
// The expectation for every negative fixture comes from its FILENAME, so the corpus cannot rot:
// adding a case is adding a file, and a fixture whose name no longer matches what the broker does
// fails immediately. The alternative -- a table in this file listing each fixture -- is a second
// place to update, and the failure mode of forgetting is a fixture that is never asserted on.
func TestFixtureCorpus(t *testing.T) {
	t.Run("valid", func(t *testing.T) {
		for _, path := range fixtures(t, "valid") {
			name := filepath.Base(path)
			if name == "identities.json" {
				continue
			}
			t.Run(name, func(t *testing.T) {
				env, err := DecodeEnvelope(read(t, path))
				if err != nil {
					t.Fatalf("valid fixture was refused: %v", err)
				}
				if env.Intent == "" || len(env.Operations) == 0 {
					t.Fatalf("decoded to an empty envelope: %+v", env)
				}
			})
		}
	})

	// Both negative directories assert the same way; only the extra side-effect expectation
	// differs, which is what separates "you sent nonsense" from "you tried something".
	for _, dir := range []string{"malformed", "spoofing"} {
		t.Run(dir, func(t *testing.T) {
			for _, path := range fixtures(t, dir) {
				name := filepath.Base(path)
				t.Run(name, func(t *testing.T) {
					wantReason, _, ok := strings.Cut(name, ".")
					if !ok {
						t.Fatalf("fixture name must be <reason>.<description>.json")
					}
					_, err := DecodeEnvelope(read(t, path))
					if err == nil {
						t.Fatalf("fixture was accepted; it must be refused with %q", wantReason)
					}
					var ref *Refusal
					if !errors.As(err, &ref) {
						t.Fatalf("refusal is not a *Refusal: %T %v", err, err)
					}
					if ref.Reason != wantReason {
						t.Fatalf("refused with %q, the filename says %q (detail: %s)", ref.Reason, wantReason, ref.Detail)
					}
					if ref.Status < 400 || ref.Status >= 500 {
						t.Fatalf("refusal status %d is not a client error", ref.Status)
					}
					if ref.Detail == "" {
						t.Fatalf("refusal carries no detail; a caller cannot act on a bare reason")
					}
					if dir != "spoofing" {
						return
					}
					// The point of the spoofing set. A reserved key that is refused quietly is a
					// reserved key that leaves no trace of having been tried.
					if !ref.Journal {
						t.Errorf("a spoofing attempt must be journaled as a Rejected record (06 §4.1)")
					}
					if !ref.SecurityEvent {
						t.Errorf("a spoofing attempt must raise a security event (06 §4.1)")
					}
				})
			}
		})
	}
}

// TestValidFixtureIdempotencyKeys pins the JCS implementation.
//
// Each valid fixture carries the key its own operations hash to under the identity in
// identities.json, so this is a golden test with no golden file: the expected value lives in the
// artifact under test. A change to the canonicaliser, the sanitizer, the sort order, or the set of
// fields in K moves at least one of these, which is exactly the blast radius those changes have in
// production -- every in-flight client's key stops matching.
func TestValidFixtureIdempotencyKeys(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join(fixtureRoot, "valid", "identities.json"))
	if err != nil {
		t.Fatalf("read identities.json: %v", err)
	}
	var identities map[string]string
	if err := json.Unmarshal(raw, &identities); err != nil {
		t.Fatalf("parse identities.json: %v", err)
	}

	for _, path := range fixtures(t, "valid") {
		name := filepath.Base(path)
		if name == "identities.json" {
			continue
		}
		t.Run(name, func(t *testing.T) {
			identity, ok := identities[name]
			if !ok {
				t.Fatalf("no entry in valid/identities.json; every valid fixture needs the identity it was submitted under")
			}
			env, err := DecodeEnvelope(read(t, path))
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			if err := CompareIdempotencyKey(identity, env); err != nil {
				t.Fatalf("%v", err)
			}
		})
	}
}

// TestSpoofingFixturesCoverEveryReservedKey closes the gap the corpus cannot close by itself: the
// fixtures prove that the keys they name are refused, and this proves there is a fixture for every
// key in the table. Without it, adding a reserved key to ReservedKeys and forgetting the fixture
// leaves a refusal nothing exercises.
func TestSpoofingFixturesCoverEveryReservedKey(t *testing.T) {
	covered := map[string]bool{}
	for _, path := range fixtures(t, "spoofing") {
		var top map[string]json.RawMessage
		if err := json.Unmarshal(read(t, path), &top); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		for k := range top {
			if _, reserved := ReservedKeys[k]; reserved {
				covered[k] = true
			}
		}
	}
	for k := range ReservedKeys {
		if !covered[k] {
			t.Errorf("no spoofing fixture carries the reserved key %q", k)
		}
	}
}

// TestReservedKeyBeatsUnknownField is the ordering V-BRK-002 depends on.
//
// DisallowUnknownFields reports whichever unknown field it reaches first and reports them all the
// same way. If the reserved-key scan ran after it, an envelope carrying `bypass: true` alongside
// any other typo would come back as a plain "unknown field" -- refused, but with no security event
// and no Rejected record. The refusal would still look correct in a browser and be wrong in the
// only place it matters.
func TestReservedKeyBeatsUnknownField(t *testing.T) {
	body := mustJSON(t, withTop(baseEnvelope(), map[string]any{
		"bypass":   true,
		"nonsense": "also unknown",
	}))
	_, err := DecodeEnvelope(body)
	var ref *Refusal
	if !errors.As(err, &ref) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if ref.Reason != ReasonBypassKey {
		t.Fatalf("reason %q; the bypass key must win over the unknown field", ref.Reason)
	}
	if !ref.SecurityEvent {
		t.Fatalf("a bypass attempt hidden behind a typo must still raise a security event")
	}
}

// TestReservedKeyRefusalIsDeterministic. Three reserved keys in one body must always name the same
// one, or the conformance check is asserting on whichever key Go's map iteration reached first.
func TestReservedKeyRefusalIsDeterministic(t *testing.T) {
	body := mustJSON(t, withTop(baseEnvelope(), map[string]any{
		"tier": "platform", "scope": "everything", "riskClass": "routine",
	}))
	var first string
	for i := 0; i < 20; i++ {
		_, err := DecodeEnvelope(body)
		var ref *Refusal
		if !errors.As(err, &ref) {
			t.Fatalf("expected a refusal, got %v", err)
		}
		key := reservedKeyOf(ref)
		if i == 0 {
			first = key
			continue
		}
		if key != first {
			t.Fatalf("iteration %d named %q, iteration 0 named %q", i, key, first)
		}
	}
	if first != "riskClass" {
		t.Fatalf("expected the sorted-first reserved key %q, got %q", "riskClass", first)
	}
}

// TestNamespaceIsReservedOnlyAtTopLevel. The single most likely over-correction in this file:
// refusing `namespace` anywhere would refuse every namespaced operation in the product.
func TestNamespaceIsReservedOnlyAtTopLevel(t *testing.T) {
	if _, err := DecodeEnvelope(mustJSON(t, baseEnvelope())); err != nil {
		t.Fatalf("a target namespace must be legal: %v", err)
	}
	_, err := DecodeEnvelope(mustJSON(t, withTop(baseEnvelope(), map[string]any{"namespace": "kube-system"})))
	var ref *Refusal
	if !errors.As(err, &ref) || ref.Reason != ReasonReservedKey {
		t.Fatalf("a top-level namespace must be refused as a reserved key, got %v", err)
	}
}

// TestPatchBodyTypeIsNotClosed. desiredState and patch.body carry arbitrary Kubernetes objects.
// If the closed-schema check reached into them, every real envelope would be refused for a field
// the broker has no business knowing about.
func TestPatchBodyTypeIsNotClosed(t *testing.T) {
	e := baseEnvelope()
	ops := e["operations"].([]any)
	ops[0] = map[string]any{
		"op":     "apply",
		"target": map[string]any{"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "name": "cfg"},
		"desiredState": map[string]any{
			"apiVersion": "v1", "kind": "ConfigMap",
			"metadata": map[string]any{"name": "cfg", "annotations": map[string]any{"anything.example.com/at-all": "yes"}},
			"data":     map[string]any{"weirdKeyTheBrokerHasNeverHeardOf": "1"},
		},
	}
	if _, err := DecodeEnvelope(mustJSON(t, e)); err != nil {
		t.Fatalf("arbitrary payload content must pass the closed schema: %v", err)
	}
}

// TestUnknownFieldIsNotASecurityEvent. The other half of the same argument: a stream that carries
// every typo is a stream nobody reads, and then the reserved-key events go unread with it.
func TestUnknownFieldIsNotASecurityEvent(t *testing.T) {
	_, err := DecodeEnvelope(mustJSON(t, withTop(baseEnvelope(), map[string]any{"intentt": "typo"})))
	var ref *Refusal
	if !errors.As(err, &ref) {
		t.Fatalf("expected a refusal, got %v", err)
	}
	if ref.Reason != ReasonUnknownField {
		t.Fatalf("reason %q, want %q", ref.Reason, ReasonUnknownField)
	}
	if ref.SecurityEvent || ref.Journal {
		t.Fatalf("a typo must not be journaled or alarmed: %+v", ref)
	}
	if ref.Status != http.StatusBadRequest {
		t.Fatalf("status %d, want 400", ref.Status)
	}
}

// TestDefaultsAreNotBakedIntoTheDecodedEnvelope. maxObjects and deadlineSeconds are pointers so an
// explicit 0 is a refusable error rather than an omission. If decode filled the defaults in, the
// envelope handed to ComputeIdempotencyKey would no longer be what the caller sent -- and the key
// is recomputed from exactly that.
func TestDefaultsAreNotBakedIntoTheDecodedEnvelope(t *testing.T) {
	env, err := DecodeEnvelope(mustJSON(t, baseEnvelope()))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if env.MaxObjects != nil || env.DeadlineSeconds != nil {
		t.Fatalf("decode must not fill defaults: maxObjects=%v deadlineSeconds=%v", env.MaxObjects, env.DeadlineSeconds)
	}
	if got := env.EffectiveMaxObjects(); got != DefaultMaxObjects {
		t.Fatalf("EffectiveMaxObjects()=%d, want %d", got, DefaultMaxObjects)
	}
	if got := env.EffectiveDeadline().Seconds(); got != DefaultDeadlineSeconds {
		t.Fatalf("EffectiveDeadline()=%v, want %ds", got, DefaultDeadlineSeconds)
	}
}

// TestParseIssuedAtRejectsOffsets. Two spellings of one instant that sort differently is a bug
// that only surfaces in an incident timeline, which is the worst time to find it.
func TestParseIssuedAtRejectsOffsets(t *testing.T) {
	if _, err := ParseIssuedAt("2026-07-24T18:02:41Z"); err != nil {
		t.Fatalf("UTC must parse: %v", err)
	}
	for _, s := range []string{"2026-07-24T14:02:41-04:00", "2026-07-24T18:02:41+00:00", "2026-07-24 18:02:41Z"} {
		if _, err := ParseIssuedAt(s); err == nil {
			t.Errorf("%q must be refused", s)
		}
	}
}

// ---- helpers ----

func fixtures(t *testing.T, dir string) []string {
	t.Helper()
	paths, err := filepath.Glob(filepath.Join(fixtureRoot, dir, "*.json"))
	if err != nil {
		t.Fatalf("glob %s: %v", dir, err)
	}
	if len(paths) == 0 {
		// A silently empty corpus is a suite that passes by testing nothing -- the same failure
		// the journal's audit-writes fixture guards against.
		t.Fatalf("no fixtures found in %s; the corpus cannot be empty", dir)
	}
	return paths
}

func read(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return b
}

func baseEnvelope() map[string]any {
	return map[string]any{
		"apiVersion": APIVersion,
		"kind":       EnvelopeKind,
		"intent":     "scale api-gateway back to four replicas",
		"operations": []any{
			map[string]any{
				"op":     "scale",
				"target": map[string]any{"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "api-gateway"},
				"scale":  map[string]any{"replicas": 4},
			},
		},
		"requester":      map[string]any{"kind": "human", "id": "slack:U02ABCDEF", "platform": "slack"},
		"trigger":        map[string]any{"source": "chat"},
		"trace":          map[string]any{"traceId": "4bf92f3577b34da6a3ce929d0e0e4736"},
		"issuedAt":       "2026-07-24T18:02:41Z",
		"nonce":          "9f2b1c7d4e6a8b0c3d5e7f9a1b2c3d4e",
		"idempotencyKey": "sha256:" + strings.Repeat("0", 64),
	}
}

func withTop(e map[string]any, extra map[string]any) map[string]any {
	for k, v := range extra {
		e[k] = v
	}
	return e
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return b
}
