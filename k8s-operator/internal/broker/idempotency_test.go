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
	"encoding/base64"
	"reflect"
	"strings"
	"testing"
)

// The idempotency key's inclusion and exclusion sets (06 §4.1).
//
// Both directions are security properties and they fail in opposite ways:
//
//   - A field wrongly INCLUDED makes a retry compute a different key and execute a second time.
//     An LLM retry rewords its own `intent` and mints a fresh `nonce` every time, so including
//     either would make the whole mechanism a no-op that still looks present.
//   - A field wrongly EXCLUDED makes two different writes collide, and the second is silently
//     deduplicated away -- a mutation the caller was told happened and which never did.
//
// So every test below is a pair: change the field, assert the key moved or did not.

// keyOf computes the key for an envelope built from a map, failing the test on any error.
func keyOf(t *testing.T, identity string, body map[string]any) string {
	t.Helper()
	env, err := DecodeEnvelope(mustJSON(t, body))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	key, err := ComputeIdempotencyKey(identity, env)
	if err != nil {
		t.Fatalf("ComputeIdempotencyKey: %v", err)
	}
	return key
}

const testAgentIdentity = "platform/adamparco-kage"

// Excluded: everything that is provenance, prose, or handling instructions.
func TestKeyExcludesEverythingButTheWrite(t *testing.T) {
	base := keyOf(t, testAgentIdentity, baseEnvelope())

	cases := []struct {
		name  string
		patch map[string]any
	}{
		// The two an LLM changes on its own retry. If either were included the mechanism would be
		// decorative: every retry would look like a new action.
		{"intent", map[string]any{"intent": "put api-gateway back to 4 replicas please"}},
		{"rationale", map[string]any{"rationale": "the on-call asked for it in #incidents"}},
		// Single-use by construction. Including it would make every key unique.
		{"nonce", map[string]any{"nonce": "1111111111111111111111111111ffff"}},
		// Provenance. The same write asked for twice from two chat threads is one write.
		{"trace", map[string]any{"trace": map[string]any{"traceId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}},
		{"requester", map[string]any{"requester": map[string]any{"kind": "agent", "id": "agent:cluster-admin"}}},
		{"trigger", map[string]any{"trigger": map[string]any{"source": "alert", "ref": "PD-991"}}},
		{"issuedAt", map[string]any{"issuedAt": "2026-07-24T19:00:00Z"}},
		// How the write is HANDLED, not what it does.
		{"requireApproval", map[string]any{"requireApproval": true}},
		{"maxObjects", map[string]any{"maxObjects": 25}},
		{"deadlineSeconds", map[string]any{"deadlineSeconds": 600}},
		// The caller's own claim about the key never feeds the key. It could not: the broker
		// recomputes over the write and compares.
		{"idempotencyKey", map[string]any{"idempotencyKey": "sha256:" + strings.Repeat("b", 64)}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := keyOf(t, testAgentIdentity, withTop(baseEnvelope(), c.patch))
			if got != base {
				t.Fatalf("changing %s moved the key\n  base: %s\n  got:  %s\n"+
					"a retry that changes only this field would execute a second time", c.name, base, got)
			}
		})
	}
}

// Included: the identity, the dry-run flag, and the operations.
func TestKeyIncludesTheWrite(t *testing.T) {
	base := keyOf(t, testAgentIdentity, baseEnvelope())

	// dryRun. A rehearsal and a real run of identical operations are emphatically not the same
	// action; deduplicating the second against the first would turn the mutation into a silent
	// no-op precisely when somebody was watching for it to happen.
	if got := keyOf(t, testAgentIdentity, withTop(baseEnvelope(), map[string]any{"dryRun": true})); got == base {
		t.Fatal("dryRun does not affect the key; a real run would be deduplicated against its own rehearsal")
	}

	// agentIdentity. Two agents issuing the same operations are two actions with two
	// accountabilities -- and if they collided, one agent could suppress another's write.
	if got := keyOf(t, "developer-team/adamparco-kage/prod/checkout", baseEnvelope()); got == base {
		t.Fatal("agentIdentity does not affect the key; one agent could suppress another's write")
	}

	// The operations themselves, field by field.
	for _, c := range []struct {
		name string
		op   map[string]any
	}{
		{"op verb", map[string]any{
			"op":     "delete",
			"target": map[string]any{"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "api-gateway"},
		}},
		{"target name", map[string]any{
			"op":     "scale",
			"target": map[string]any{"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "api-gateway-canary"},
			"scale":  map[string]any{"replicas": 4},
		}},
		{"target namespace", map[string]any{
			"op":     "scale",
			"target": map[string]any{"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "payments", "name": "api-gateway"},
			"scale":  map[string]any{"replicas": 4},
		}},
		{"target kind", map[string]any{
			"op":     "scale",
			"target": map[string]any{"group": "apps", "version": "v1", "kind": "StatefulSet", "namespace": "checkout", "name": "api-gateway"},
			"scale":  map[string]any{"replicas": 4},
		}},
		{"replica count", map[string]any{
			"op":     "scale",
			"target": map[string]any{"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "checkout", "name": "api-gateway"},
			"scale":  map[string]any{"replicas": 40},
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			body := withTop(baseEnvelope(), map[string]any{"operations": []any{c.op}})
			if got := keyOf(t, testAgentIdentity, body); got == base {
				t.Fatalf("changing the %s does not move the key; two different writes would collide and the second would be silently deduplicated", c.name)
			}
		})
	}
}

// Operation ORDER does not matter, but the operation SET does.
//
// Two envelopes applying the same three patches in a different order are the same write, and an
// LLM retry reorders them. Sorting is what makes that true. But the sort must not lose an
// operation: a key computed over a set that dropped one describes a write nobody performed.
func TestKeyIsOrderIndependentButSetSensitive(t *testing.T) {
	a := map[string]any{
		"op":     "patch",
		"target": map[string]any{"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "name": "alpha"},
		"patch":  map[string]any{"type": "application/merge-patch+json", "body": map[string]any{"data": map[string]any{"k": "1"}}},
	}
	b := map[string]any{
		"op":     "patch",
		"target": map[string]any{"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "name": "beta"},
		"patch":  map[string]any{"type": "application/merge-patch+json", "body": map[string]any{"data": map[string]any{"k": "2"}}},
	}
	c := map[string]any{
		"op":     "delete",
		"target": map[string]any{"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "name": "gamma"},
	}

	forward := keyOf(t, testAgentIdentity, withTop(baseEnvelope(), map[string]any{"operations": []any{a, b, c}}))
	reversed := keyOf(t, testAgentIdentity, withTop(baseEnvelope(), map[string]any{"operations": []any{c, b, a}}))
	shuffled := keyOf(t, testAgentIdentity, withTop(baseEnvelope(), map[string]any{"operations": []any{b, c, a}}))

	if forward != reversed || forward != shuffled {
		t.Fatalf("reordering the operations moved the key:\n  abc: %s\n  cba: %s\n  bca: %s", forward, reversed, shuffled)
	}
	if dropped := keyOf(t, testAgentIdentity, withTop(baseEnvelope(), map[string]any{"operations": []any{a, b}})); dropped == forward {
		t.Fatal("dropping an operation did not move the key; the key would describe a write that was not performed")
	}
}

// Two operations differing only in their label selector must not sort equal.
//
// If they did, the sort's stability would leak the caller's original order back into the key --
// which is the exact property TestKeyIsOrderIndependentButSetSensitive says must not hold.
func TestSelectorOperationsSortDistinctly(t *testing.T) {
	mk := func(sel string) keyOperation {
		return keyOperation{Op: "delete", TargetSelector: &TargetSelector{
			Version: "v1", Kind: "Pod", Namespace: "checkout", LabelSelector: sel,
		}}
	}
	if operationSortKey(mk("app=a")) == operationSortKey(mk("app=b")) {
		t.Fatal("two selectors sort identically; the caller's ordering would leak into the key")
	}

	// Same for cloud targets, which also have no name.
	c1 := keyOperation{Op: "apply", CloudTarget: &CloudTarget{Provider: "gcp", Service: "compute", Resource: "instance-1", Method: "insert"}}
	c2 := keyOperation{Op: "apply", CloudTarget: &CloudTarget{Provider: "gcp", Service: "compute", Resource: "instance-1", Method: "delete"}}
	if operationSortKey(c1) == operationSortKey(c2) {
		t.Fatal("two cloud methods on one resource sort identically")
	}

	// The op verb is separated from the GVKNN by US (0x1F), not by the `/` that joins the rest.
	// That boundary is the one that has to be unambiguous, because `op` and `group` are drawn from
	// different alphabets and a shared separator would let `delete` + group `x` collide with a verb
	// that happened to end in the separator.
	//
	// The remaining slots are `/`-joined, exactly as 06 §4.1 specifies, and that IS ambiguous in
	// principle -- "a/b" in one slot and "b" in the next produce the same string as "a" and "b/b".
	// It is safe only because the four non-terminal slots cannot contain a `/`: group and namespace
	// are DNS names, version and kind are alphanumeric. The fields that genuinely can -- a label
	// selector like `app.kubernetes.io/name=x`, a GCP resource path -- all land in the terminal
	// slot, where nothing follows them to blend into. Pinned here so that moving a `/`-bearing
	// field out of the last position fails a test rather than quietly tying two sort keys.
	last := keyOperation{Op: "delete", Target: &Target{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "ns", Name: "a/b"}}
	if got := operationSortKey(last); !strings.HasSuffix(got, "/a/b") {
		t.Fatalf("sort key %q does not end in the name slot; a `/`-bearing field is no longer terminal", got)
	}
	if verb, rest, found := strings.Cut(operationSortKey(last), "\x1f"); !found || verb != "delete" || strings.Contains(verb, "/") {
		t.Fatalf("the op verb is not US-separated from the GVKNN: %q / %q", verb, rest)
	}
}

// Secret material never enters the key.
//
// The key is a SHA-256 over the canonical form, so in principle a credential in it is not
// recoverable -- but the key is also a low-entropy-input oracle: an attacker who can compute keys
// offline for candidate Secret values and compare them against a journaled key has a confirmation
// channel. The sanitizer removes the question.
func TestSecretPayloadsAreDigestedBeforeHashing(t *testing.T) {
	secretEnv := func(value string) map[string]any {
		return withTop(baseEnvelope(), map[string]any{"operations": []any{map[string]any{
			"op":     "apply",
			"target": map[string]any{"version": "v1", "kind": "Secret", "namespace": "checkout", "name": "db-credentials"},
			"desiredState": map[string]any{
				"apiVersion": "v1",
				"kind":       "Secret",
				"metadata":   map[string]any{"name": "db-credentials", "namespace": "checkout"},
				"data":       map[string]any{"password": base64.StdEncoding.EncodeToString([]byte(value))},
			},
		}}})
	}

	// Different material still produces different keys -- the digest is per key, so changing the
	// password IS a different write and must not deduplicate against the old one.
	if keyOf(t, testAgentIdentity, secretEnv("hunter2")) == keyOf(t, testAgentIdentity, secretEnv("hunter3")) {
		t.Fatal("two different Secret values produced the same key; rotating a credential would be deduplicated away")
	}

	// And the material itself is nowhere in the canonical input. Asserted on the reduced operation
	// rather than the key, because the key is a hash and could hide it.
	env, err := DecodeEnvelope(mustJSON(t, secretEnv("hunter2")))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	reduced, err := reduceForKey(&env.Operations[0])
	if err != nil {
		t.Fatalf("reduceForKey: %v", err)
	}
	rendered := mustJSON(t, reduced)
	for _, forbidden := range []string{"hunter2", base64.StdEncoding.EncodeToString([]byte("hunter2"))} {
		if strings.Contains(string(rendered), forbidden) {
			t.Fatalf("credential material survived into the key input: %s", rendered)
		}
	}
}

// The kind-injection round trip. journal.Sanitize decides whether to digest `data` by reading the
// object's own `kind`, and a merge-patch body is `{"data":{...}}` with no kind at all. The kind is
// injected from the target, and removed again -- so a payload that DECLARES its kind and one that
// relies on the target produce the same key for the same write.
func TestPayloadKindIsInjectedForSanitisingAndRemovedAfter(t *testing.T) {
	target := map[string]any{"version": "v1", "kind": "Secret", "namespace": "checkout", "name": "db-credentials"}
	encoded := base64.StdEncoding.EncodeToString([]byte("hunter2"))

	// A merge patch with no kind. Without the injection this would be hashed undigested.
	patchOp := map[string]any{
		"op":     "patch",
		"target": target,
		"patch": map[string]any{
			"type": "application/merge-patch+json",
			"body": map[string]any{"data": map[string]any{"password": encoded}},
		},
	}
	env, err := DecodeEnvelope(mustJSON(t, withTop(baseEnvelope(), map[string]any{"operations": []any{patchOp}})))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	reduced, err := reduceForKey(&env.Operations[0])
	if err != nil {
		t.Fatalf("reduceForKey: %v", err)
	}
	body, ok := reduced.Patch.Body.(map[string]any)
	if !ok {
		t.Fatalf("patch body is %T, want a map", reduced.Patch.Body)
	}
	if strings.Contains(string(mustJSON(t, body)), encoded) {
		t.Fatalf("a kindless Secret merge patch was hashed undigested: %s", mustJSON(t, body))
	}
	// The injected kind must not survive into the key input.
	if _, present := body["kind"]; present {
		t.Fatalf("the injected kind leaked into the key input: %s", mustJSON(t, body))
	}
}

// A JSON Patch body is an ARRAY, which the object sanitizer has no shape for. Values written under
// a Secret's /data or /stringData are digested individually, by path -- this is the one place the
// redaction is applied by path rather than by field, because in a JSON Patch the field name lives
// in the `path` string and nowhere else.
func TestJSONPatchSecretValuesAreDigestedByPath(t *testing.T) {
	encoded := base64.StdEncoding.EncodeToString([]byte("hunter2"))
	op := map[string]any{
		"op":     "patch",
		"target": map[string]any{"version": "v1", "kind": "Secret", "namespace": "checkout", "name": "db-credentials"},
		"patch": map[string]any{
			"type": "application/json-patch+json",
			"body": []any{
				map[string]any{"op": "replace", "path": "/data/password", "value": encoded},
				// Not credential material: a label is a label, and digesting it would make the key
				// blind to a change that genuinely is a different write.
				map[string]any{"op": "add", "path": "/metadata/labels/rotated", "value": "2026-07-27"},
				// A remove names a key but carries no material.
				map[string]any{"op": "remove", "path": "/data/legacy"},
			},
		},
	}
	env, err := DecodeEnvelope(mustJSON(t, withTop(baseEnvelope(), map[string]any{"operations": []any{op}})))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	reduced, err := reduceForKey(&env.Operations[0])
	if err != nil {
		t.Fatalf("reduceForKey: %v", err)
	}
	entries, ok := reduced.Patch.Body.([]any)
	if !ok {
		t.Fatalf("patch body is %T, want an array", reduced.Patch.Body)
	}
	if len(entries) != 3 {
		t.Fatalf("the sanitizer changed the operation count: %d, want 3", len(entries))
	}

	first := entries[0].(map[string]any)
	if v, _ := first["value"].(string); v == encoded {
		t.Fatal("a /data value survived undigested")
	} else if !strings.HasPrefix(v, KeyPrefix) {
		t.Fatalf("the digested value is %q, want a %s digest", v, KeyPrefix)
	}
	second := entries[1].(map[string]any)
	if second["value"] != "2026-07-27" {
		t.Fatalf("a label value was digested: %v", second["value"])
	}
	third := entries[2].(map[string]any)
	if _, present := third["value"]; present {
		t.Fatal("the sanitizer invented a value for a remove op")
	}

	// A non-Secret target leaves a JSON Patch alone entirely.
	cmOp := map[string]any{
		"op":     "patch",
		"target": map[string]any{"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "name": "settings"},
		"patch": map[string]any{
			"type": "application/json-patch+json",
			"body": []any{map[string]any{"op": "replace", "path": "/data/level", "value": "debug"}},
		},
	}
	cmEnv, err := DecodeEnvelope(mustJSON(t, withTop(baseEnvelope(), map[string]any{"operations": []any{cmOp}})))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	cmReduced, err := reduceForKey(&cmEnv.Operations[0])
	if err != nil {
		t.Fatalf("reduceForKey: %v", err)
	}
	got := cmReduced.Patch.Body.([]any)[0].(map[string]any)
	if got["value"] != "debug" {
		t.Fatalf("a ConfigMap value was digested: %v", got["value"])
	}
}

// The key is deterministic across runs. Go's map iteration is randomised, so an implementation that
// leaked map order into the canonical input would pass a single run and fail intermittently in CI
// -- and, worse, would compute a different key for the same write on two different broker restarts.
func TestKeyIsDeterministic(t *testing.T) {
	body := withTop(baseEnvelope(), map[string]any{"operations": []any{map[string]any{
		"op":     "apply",
		"target": map[string]any{"version": "v1", "kind": "ConfigMap", "namespace": "checkout", "name": "settings"},
		"desiredState": map[string]any{
			"apiVersion": "v1", "kind": "ConfigMap",
			"metadata": map[string]any{"name": "settings", "namespace": "checkout"},
			"data": map[string]any{
				"a": "1", "b": "2", "c": "3", "d": "4", "e": "5",
				"f": "6", "g": "7", "h": "8", "i": "9", "j": "10",
			},
		},
	}}})

	first := keyOf(t, testAgentIdentity, body)
	for i := 0; i < 64; i++ {
		if got := keyOf(t, testAgentIdentity, body); got != first {
			t.Fatalf("run %d produced %s, first run produced %s; map order is leaking into the key", i, got, first)
		}
	}
	if !sha256KeyRe.MatchString(first) {
		t.Fatalf("key %q is not sha256:<64 lowercase hex>", first)
	}
}

// An empty identity is an error, not a key. A key computed over an empty identity would collide
// across every agent in the cluster -- so this must fail loudly rather than produce a value.
func TestKeyRefusesAnEmptyIdentity(t *testing.T) {
	env, err := DecodeEnvelope(mustJSON(t, baseEnvelope()))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if key, err := ComputeIdempotencyKey("", env); err == nil {
		t.Fatalf("computed %s for an empty identity", key)
	}
}

// CompareIdempotencyKey's refusal carries the reason the caller needs and NOT the flags an attack
// would carry: a mismatch is a client bug, and alarming on every one would bury the reserved-key
// events that are real.
func TestCompareIdempotencyKeyRefusal(t *testing.T) {
	env, err := DecodeEnvelope(mustJSON(t, withTop(baseEnvelope(), map[string]any{
		"idempotencyKey": "sha256:" + strings.Repeat("c", 64),
	})))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	ref := refusalOf(t, CompareIdempotencyKey(testAgentIdentity, env))
	if ref.Reason != ReasonIdempotencyKeyMismatch {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonIdempotencyKeyMismatch)
	}
	if ref.Journal || ref.SecurityEvent {
		t.Fatalf("a key mismatch must be neither journaled nor alarmed; Journal=%v SecurityEvent=%v", ref.Journal, ref.SecurityEvent)
	}
	// The detail names both values, so a client with a canonicalisation bug can see the difference
	// rather than guess at it. A mismatch that only says "mismatch" is unactionable.
	if !strings.Contains(ref.Detail, env.IdempotencyKey) {
		t.Fatalf("the refusal does not quote the key that was sent: %s", ref.Detail)
	}

	// The correct key is accepted.
	correct, err := ComputeIdempotencyKey(testAgentIdentity, env)
	if err != nil {
		t.Fatalf("ComputeIdempotencyKey: %v", err)
	}
	env.IdempotencyKey = correct
	if err := CompareIdempotencyKey(testAgentIdentity, env); err != nil {
		t.Fatalf("the recomputed key was refused: %v", err)
	}
}

// keyOperation is a separate type from Operation on purpose: a new field on the wire envelope has
// to be added HERE, deliberately, to become part of the key. If the two were the same type -- or if
// keyOperation grew a catch-all -- adding an envelope field would silently change every key in
// flight and deduplicate a fleet's worth of retries against the wrong records.
//
// This test does not stop the field being added. It makes adding one a decision somebody wrote
// down, by failing until the count here is updated.
func TestKeyOperationFieldSetIsPinned(t *testing.T) {
	want := []string{"Op", "Target", "TargetSelector", "CloudTarget", "DesiredState", "Patch", "Delete", "Scale"}
	tp := reflect.TypeOf(keyOperation{})
	got := make([]string, 0, tp.NumField())
	for i := 0; i < tp.NumField(); i++ {
		got = append(got, tp.Field(i).Name)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("keyOperation fields changed:\n  got:  %v\n  want: %v\n"+
			"every idempotency key in flight changes with this type; update the expectation only if that is intended", got, want)
	}

	// And keyInput, which is the K of 06 §4.1.
	ki := reflect.TypeOf(keyInput{})
	wantK := []string{"AgentIdentity", "DryRun", "Operations"}
	gotK := make([]string, 0, ki.NumField())
	for i := 0; i < ki.NumField(); i++ {
		gotK = append(gotK, ki.Field(i).Name)
	}
	if !reflect.DeepEqual(gotK, wantK) {
		t.Fatalf("keyInput fields changed:\n  got:  %v\n  want: %v", gotK, wantK)
	}
	// The JSON names are the contract, not the Go names -- JCS hashes the JSON.
	for i, name := range []string{"agentIdentity", "dryRun", "operations"} {
		if tag := ki.Field(i).Tag.Get("json"); tag != name {
			t.Fatalf("keyInput field %d has json tag %q, want %q; renaming it changes every key", i, tag, name)
		}
	}
}
