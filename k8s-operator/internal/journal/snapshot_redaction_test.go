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

// V-OBS-008 -- a journaled Secret pre-state is digest-only, and the replayer recognises it as such.
//
// 05 §1.2: "Snapshots are stripped of managedFields and of Secret data (a Secret's pre-state is
// recorded as a per-key digest, never as material -- undoing a Secret change restores from the
// digest-matched value only if the broker still holds it in the sink under the sink's own
// encryption)."
//
// The first half of that sentence is asserted three times already, field by field, in
// snapshot_test.go. This file asserts the two things those tests cannot see from where they stand.
//
// The first is that no material survives ANYWHERE in the bytes that reach etcd. The existing tests
// read named fields back out of the sanitized map; a value that escaped into an annotation, a
// label, an ownerReference or a nested list would satisfy every one of them. What the journal
// stores is `json.Marshal(clean.Object)`, so that is what this file searches.
//
// The second is the seam. `journal.Sanitize` WRITES the marker `sha256:` into a Secret's values;
// `undo.RedactedSecretKeys` -- via rollback.go's refusal -- READS it, and refuses to replay a body
// carrying it. Those are two packages, and until this file nothing joined them: each side's doc
// comment states the contract from its own end and neither is a test. If the journal's marker
// drifted, the replayer would not refuse. It would succeed, writing the hex of each value's own
// digest into the live Secret, breaking every pod that mounts it -- and reporting a completed undo.
// That failure is invisible to both packages' own suites, which is exactly why it belongs here.
//
// Kept as an external test package so it may import `undo`, which imports `journal`.
package journal_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// The plaintexts the fixture is built from. `password` and `dup` deliberately hold the SAME value
// under two keys, which is what makes "per-key" a claim with an observable consequence rather than a
// phrasing: it has to produce equal digests there and unequal ones elsewhere.
const (
	passwordPlain = "hunter2-correct-horse"
	usernamePlain = "svc-migrations"
	tokenPlain    = "t0ps3cret-bearer-material"
)

// redactionFixture is a Secret carrying material in every place 05 §1.2 cares about: `data`,
// `stringData`, and the last-applied annotation that is an undigested second copy of both.
func redactionFixture() *unstructured.Unstructured {
	lastApplied := `{"data":{"password":"` + base64.StdEncoding.EncodeToString([]byte(passwordPlain)) + `"}}`
	return &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata": map[string]interface{}{
			"name":      "db-credentials",
			"namespace": "team-x",
			"annotations": map[string]interface{}{
				"kubectl.kubernetes.io/last-applied-configuration": lastApplied,
				"team": "x",
			},
			"managedFields": []interface{}{
				map[string]interface{}{"manager": "kubectl", "operation": "Apply"},
			},
		},
		"data": map[string]interface{}{
			"password": base64.StdEncoding.EncodeToString([]byte(passwordPlain)),
			"username": base64.StdEncoding.EncodeToString([]byte(usernamePlain)),
			"dup":      base64.StdEncoding.EncodeToString([]byte(passwordPlain)),
		},
		"stringData": map[string]interface{}{"token": tokenPlain},
	}}
}

// needles is every byte sequence that must not appear in a journaled body.
//
// These are the STORED forms, not the plaintexts: a Secret's `data` holds base64, so the raw
// password is not in the fixture's bytes to begin with and asserting its absence would assert
// nothing. `stringData` is the one field that does hold text as written.
func needles() []string {
	return []string{
		base64.StdEncoding.EncodeToString([]byte(passwordPlain)),
		base64.StdEncoding.EncodeToString([]byte(usernamePlain)),
		tokenPlain,
	}
}

func TestJournaledSecretBodyCarriesNoMaterialAnywhere(t *testing.T) {
	obj := redactionFixture()

	// The vacuity guard, and it is not decoration. Searching a body for strings that were never in
	// it is a test that passes on an empty implementation, and this one searches for six.
	before, err := json.Marshal(obj.Object)
	if err != nil {
		t.Fatalf("marshal fixture: %v", err)
	}
	for _, n := range needles() {
		if !bytes.Contains(before, []byte(n)) {
			t.Fatalf("the fixture does not contain %q before sanitization, so finding it absent afterwards proves nothing", n)
		}
	}

	clean, err := journal.Sanitize(obj)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	// These are the exact bytes journal.Snapshot digests and stores -- inline in spec.preState, or
	// in the blob sink. Anything readable here is readable by every identity that may run
	// `kubectl get actionrecord -o yaml`.
	body, err := json.Marshal(clean.Object)
	if err != nil {
		t.Fatalf("marshal sanitized: %v", err)
	}
	for _, n := range needles() {
		if bytes.Contains(body, []byte(n)) {
			t.Fatalf("secret material %q survives in the journaled body; 05 §1.2 records a Secret's "+
				"pre-state as a per-key digest, never as material. Body: %s", n, body)
		}
	}
	if bytes.Contains(body, []byte("managedFields")) {
		t.Fatalf("managedFields survives in the journaled body: %s", body)
	}
	if bytes.Contains(body, []byte("last-applied-configuration")) {
		t.Fatalf("the last-applied annotation survives; it is an undigested second copy of the Secret: %s", body)
	}
	// The snapshot must still be an object worth restoring from.
	if clean.GetName() != "db-credentials" || clean.GetNamespace() != "team-x" {
		t.Fatalf("sanitization lost the object's identity: %s/%s", clean.GetNamespace(), clean.GetName())
	}
	if v, _, _ := unstructured.NestedString(clean.Object, "metadata", "annotations", "team"); v != "x" {
		t.Fatalf("an unrelated annotation was stripped: %q", v)
	}
}

func TestJournalRedactionIsRecognisedByTheReplayer(t *testing.T) {
	obj := redactionFixture()

	// The replayer must NOT think an unsanitized Secret is redacted -- if it did, this test would
	// pass no matter what marker the journal wrote, and the refusal would be a refusal of
	// everything.
	if keys := undo.RedactedSecretKeys(obj); len(keys) != 0 {
		t.Fatalf("the unsanitized fixture is already read as redacted (%v); the marker check is matching material", keys)
	}

	clean, err := journal.Sanitize(obj)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}

	got := undo.RedactedSecretKeys(clean)
	want := []string{"data[dup]", "data[password]", "data[username]", "stringData[token]"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("the rollback replayer sees %v as digest placeholders, want %v.\n"+
			"journal.Sanitize writes the marker and undo.RedactedSecretKeys reads it, across a "+
			"package boundary neither side's suite crosses. When they disagree the replay does not "+
			"fail -- it applies the digests as if they were values, destroying the credential and "+
			"reporting a completed undo (rollback.go's REFUSING branch is what this feeds).", got, want)
	}
}

func TestJournalSecretDigestIsPerKeyOverTheStoredValue(t *testing.T) {
	clean, err := journal.Sanitize(redactionFixture())
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}

	// Recomputed here from the spec's own words rather than by calling journal.Digest, so a change
	// to the digest function is a failure rather than a pair of matching answers.
	wantDigest := func(stored string) string {
		sum := sha256.Sum256([]byte(stored))
		return "sha256:" + hex.EncodeToString(sum[:])
	}

	data, found, err := unstructured.NestedStringMap(clean.Object, "data")
	if err != nil || !found {
		t.Fatalf("data missing after sanitization (found=%v err=%v); undo needs to know which keys existed", found, err)
	}
	strData, found, err := unstructured.NestedStringMap(clean.Object, "stringData")
	if err != nil || !found {
		t.Fatalf("stringData missing after sanitization (found=%v err=%v)", found, err)
	}

	for key, plain := range map[string]string{"password": passwordPlain, "username": usernamePlain, "dup": passwordPlain} {
		stored := base64.StdEncoding.EncodeToString([]byte(plain))
		if got := data[key]; got != wantDigest(stored) {
			t.Fatalf("data[%s] = %q, want the digest of the value it replaced (%q)", key, got, wantDigest(stored))
		}
	}
	if got := strData["token"]; got != wantDigest(tokenPlain) {
		t.Fatalf("stringData[token] = %q, want %q", got, wantDigest(tokenPlain))
	}

	// Per-key, with the two consequences that phrase has: different values digest differently, so
	// undo can tell which key changed; and equal values digest equally, so a candidate value can be
	// proved to be the right one for a key.
	if data["password"] == data["username"] {
		t.Fatalf("two different Secret values produced the same digest (%q); undo cannot tell which key changed", data["password"])
	}
	if data["password"] != data["dup"] {
		t.Fatalf("the same Secret value under two keys produced different digests (%q vs %q); a "+
			"digest-matched restore could never prove a candidate value is the right one",
			data["password"], data["dup"])
	}
}
