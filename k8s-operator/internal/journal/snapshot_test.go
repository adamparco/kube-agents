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

package journal

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

var capturedAt = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

// memBlob is a BlobSink that can be made to fail, because the property that matters most here --
// "a snapshot that cannot be persisted must fail the action" -- is unobservable against a sink that
// always succeeds.
type memBlob struct {
	mu      sync.Mutex
	objects map[string][]byte
	putErr  error
	// lie makes Put return a digest that does not match the body, standing in for a sink that
	// silently corrupts or truncates.
	lie string
}

func newMemBlob() *memBlob { return &memBlob{objects: map[string][]byte{}} }

func (m *memBlob) Name() string { return "mem" }

func (m *memBlob) Put(_ context.Context, key string, body []byte) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.putErr != nil {
		return "", m.putErr
	}
	m.objects[key] = append([]byte(nil), body...)
	if m.lie != "" {
		return m.lie, nil
	}
	return Digest(body), nil
}

func (m *memBlob) Get(_ context.Context, key string) ([]byte, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	body, ok := m.objects[key]
	if !ok {
		return nil, fmt.Errorf("mem: no object at %q", key)
	}
	return append([]byte(nil), body...), nil
}

func deployment(extra map[string]interface{}) *unstructured.Unstructured {
	obj := map[string]interface{}{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata": map[string]interface{}{
			"name":      "api-gateway",
			"namespace": "team-x",
		},
		"spec": map[string]interface{}{"replicas": int64(3)},
	}
	for k, v := range extra {
		obj[k] = v
	}
	return &unstructured.Unstructured{Object: obj}
}

func TestSanitizeStripsManagedFields(t *testing.T) {
	obj := deployment(nil)
	meta, _, _ := unstructured.NestedMap(obj.Object, "metadata")
	meta["managedFields"] = []interface{}{map[string]interface{}{"manager": "kubectl", "operation": "Apply"}}
	if err := unstructured.SetNestedMap(obj.Object, meta, "metadata"); err != nil {
		t.Fatalf("set metadata: %v", err)
	}

	got, err := Sanitize(obj)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	if _, found, _ := unstructured.NestedFieldNoCopy(got.Object, "metadata", "managedFields"); found {
		t.Fatal("managedFields survived sanitization; server-side-apply bookkeeping can dwarf the object and would push honest snapshots over the inline limit")
	}
	// Sanitize must not mutate the caller's object: the broker holds the live object for other
	// purposes, and a snapshot routine that quietly edits it is a bug found much later.
	if _, found, _ := unstructured.NestedFieldNoCopy(obj.Object, "metadata", "managedFields"); !found {
		t.Fatal("Sanitize mutated the object it was handed")
	}
	if got.GetName() != "api-gateway" {
		t.Fatalf("sanitization lost the object identity: name = %q", got.GetName())
	}
}

func TestSanitizeStripsLastAppliedConfiguration(t *testing.T) {
	// This annotation is a second full copy of the object, and for a Secret it is a copy that has
	// NOT been through the digesting below. Leaving it would make the Secret redaction cosmetic --
	// which is the whole point of testing it on a Secret rather than a Deployment.
	obj := &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata": map[string]interface{}{
			"name":      "db-credentials",
			"namespace": "team-x",
			"annotations": map[string]interface{}{
				"kubectl.kubernetes.io/last-applied-configuration": `{"data":{"password":"aHVudGVyMg=="}}`,
				"team": "x",
			},
		},
		"data": map[string]interface{}{"password": base64.StdEncoding.EncodeToString([]byte("hunter2"))},
	}}

	got, err := Sanitize(obj)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	rendered := fmt.Sprint(got.Object)
	if strings.Contains(rendered, "aHVudGVyMg==") || strings.Contains(rendered, "hunter2") {
		t.Fatalf("secret material survived sanitization: %s", rendered)
	}
	if _, found, _ := unstructured.NestedString(got.Object, "metadata", "annotations", "kubectl.kubernetes.io/last-applied-configuration"); found {
		t.Fatal("last-applied-configuration survived; it is an undigested second copy of the Secret")
	}
	// Unrelated annotations must survive -- an undo that restored an object without its annotations
	// would be a different object.
	if v, _, _ := unstructured.NestedString(got.Object, "metadata", "annotations", "team"); v != "x" {
		t.Fatalf("an unrelated annotation was stripped: %q", v)
	}
}

func TestSanitizeDigestsSecretDataPerKey(t *testing.T) {
	// Per-key rather than wholesale removal: undo needs to know WHICH keys changed and be able to
	// prove a candidate value is the right one, and a single digest over the whole map cannot do
	// either. `kubectl get actionrecord -o yaml` is readable by every reader identity, so this is
	// the line between a journal and a credential-exfiltration path.
	obj := &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]interface{}{"name": "db-credentials", "namespace": "team-x"},
		"data": map[string]interface{}{
			"password": base64.StdEncoding.EncodeToString([]byte("hunter2")),
			"username": base64.StdEncoding.EncodeToString([]byte("admin")),
		},
		"stringData": map[string]interface{}{"token": "t0ps3cret"},
	}}

	got, err := Sanitize(obj)
	if err != nil {
		t.Fatalf("Sanitize: %v", err)
	}
	for _, field := range []string{"data", "stringData"} {
		m, found, err := unstructured.NestedStringMap(got.Object, field)
		if err != nil || !found {
			t.Fatalf("%s missing after sanitization (found=%v err=%v); undo needs to know which keys existed", field, found, err)
		}
		for k, v := range m {
			if !strings.HasPrefix(v, "sha256:") || len(v) != len("sha256:")+64 {
				t.Fatalf("%s[%q] = %q, want a sha256: digest", field, k, v)
			}
		}
	}
	rendered := fmt.Sprint(got.Object)
	for _, material := range []string{"hunter2", "admin", "t0ps3cret", base64.StdEncoding.EncodeToString([]byte("hunter2"))} {
		if strings.Contains(rendered, material) {
			t.Fatalf("secret material %q survived sanitization", material)
		}
	}
}

func TestSnapshotInlinesSmallBodies(t *testing.T) {
	sink := newMemBlob()
	snap, err := Snapshot(context.Background(), sink, 0, deployment(nil), SnapshotKeyPrefix("cluster-admin/p/c", "01JZQ8X9K7M4N2P6R8T0V3W5YZ"), capturedAt)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if snap.Object == nil {
		t.Fatal("a small body was not inlined; every undo would then need a round trip to the blob sink")
	}
	if snap.ObjectRef != nil {
		t.Fatal("a small body was inlined AND referenced; the CRD's exactly-one rule rejects that record")
	}
	if snap.SHA256 != Digest(snap.Object.Raw) {
		t.Fatalf("digest %q does not match the inlined body", snap.SHA256)
	}
	if len(sink.objects) != 0 {
		t.Fatalf("a small body was written to the blob sink anyway: %d object(s)", len(sink.objects))
	}
	if !snap.CapturedAt.UTC().Equal(capturedAt) {
		t.Fatalf("CapturedAt = %s, want %s", snap.CapturedAt.UTC(), capturedAt)
	}
}

// bigObject builds an object whose serialized form exceeds the inline limit.
func bigObject() *unstructured.Unstructured {
	return deployment(map[string]interface{}{
		"data": map[string]interface{}{"blob": strings.Repeat("x", InlineSnapshotLimit+4096)},
	})
}

func TestSnapshotSendsLargeBodiesToTheSink(t *testing.T) {
	sink := newMemBlob()
	prefix := SnapshotKeyPrefix("cluster-admin/p/c", "01JZQ8X9K7M4N2P6R8T0V3W5YZ")
	snap, err := Snapshot(context.Background(), sink, 2, bigObject(), prefix, capturedAt)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if snap.Object != nil {
		t.Fatalf("a >1 MiB body was inlined; etcd's object limit is 1.5 MiB and a record carries one snapshot PER TARGET")
	}
	if snap.ObjectRef == nil {
		t.Fatal("a large body produced neither an inline object nor a reference")
	}
	if snap.ObjectRef.Store != sink.Name() {
		t.Fatalf("ObjectRef.Store = %q, want %q -- a reader has to know where to go looking", snap.ObjectRef.Store, sink.Name())
	}
	if !strings.HasPrefix(snap.ObjectRef.Key, prefix) {
		t.Fatalf("ObjectRef.Key = %q, want it under the prefix %q", snap.ObjectRef.Key, prefix)
	}
	body, err := sink.Get(context.Background(), snap.ObjectRef.Key)
	if err != nil {
		t.Fatalf("the referenced body is not in the sink: %v", err)
	}
	if Digest(body) != snap.SHA256 || snap.ObjectRef.SHA256 != snap.SHA256 {
		t.Fatal("the digests on the snapshot, the reference and the stored body disagree")
	}
}

func TestSnapshotFailsClosedWhenTheSinkCannotStore(t *testing.T) {
	// 03 §6 applied to the snapshot: an action executed without a recoverable pre-state is an action
	// that cannot be undone, recorded on a document that says it can. Returning a snapshot with an
	// empty ref and no error would be the quiet version of that.
	sink := newMemBlob()
	sink.putErr = errors.New("bucket is not writable")
	if _, err := Snapshot(context.Background(), sink, 0, bigObject(), "p/", capturedAt); err == nil {
		t.Fatal("Snapshot succeeded despite the blob sink refusing the body")
	}
}

func TestSnapshotRefusesAnOversizedBodyWithNoSink(t *testing.T) {
	// Nil sink is legal for a deployment that never produces large snapshots. The failure mode this
	// forbids is inlining a 1 MiB body "because there was nowhere else to put it".
	if _, err := Snapshot(context.Background(), nil, 0, bigObject(), "p/", capturedAt); err == nil {
		t.Fatal("a >1 MiB body was accepted with no blob sink configured")
	}
	// ...but a small body must still work, or a nil sink would break every ordinary action.
	if _, err := Snapshot(context.Background(), nil, 0, deployment(nil), "p/", capturedAt); err != nil {
		t.Fatalf("a small body failed with no sink configured: %v", err)
	}
}

func TestSnapshotRejectsASinkThatMisreportsItsDigest(t *testing.T) {
	// The digest in the CR is the only thing standing between undo and replaying whatever the sink
	// happens to hold at that key later. Trusting the sink's returned digest without checking it
	// against the bytes we sent would make the check circular.
	sink := newMemBlob()
	sink.lie = strings.Repeat("b", 64)
	if _, err := Snapshot(context.Background(), sink, 0, bigObject(), "p/", capturedAt); err == nil {
		t.Fatal("a sink that returned the wrong digest was believed")
	}
}

func TestLoadSnapshotVerifiesTheDigestBothWays(t *testing.T) {
	ctx := context.Background()
	sink := newMemBlob()

	t.Run("inline round trip", func(t *testing.T) {
		snap, err := Snapshot(ctx, sink, 0, deployment(nil), "p/", capturedAt)
		if err != nil {
			t.Fatalf("Snapshot: %v", err)
		}
		body, err := LoadSnapshot(ctx, sink, snap)
		if err != nil {
			t.Fatalf("LoadSnapshot: %v", err)
		}
		if Digest(body) != snap.SHA256 {
			t.Fatal("LoadSnapshot returned a body that does not match the recorded digest")
		}
	})

	t.Run("stored round trip", func(t *testing.T) {
		snap, err := Snapshot(ctx, sink, 0, bigObject(), "p/", capturedAt)
		if err != nil {
			t.Fatalf("Snapshot: %v", err)
		}
		if _, err := LoadSnapshot(ctx, sink, snap); err != nil {
			t.Fatalf("LoadSnapshot: %v", err)
		}
	})

	t.Run("an edited inline body is refused", func(t *testing.T) {
		// spec is immutable by CEL, so a mismatch here means something got past that -- exactly the
		// case where a silent restore of the wrong world would be worst.
		snap, err := Snapshot(ctx, sink, 0, deployment(nil), "p/", capturedAt)
		if err != nil {
			t.Fatalf("Snapshot: %v", err)
		}
		snap.SHA256 = strings.Repeat("c", 64)
		if _, err := LoadSnapshot(ctx, sink, snap); err == nil {
			t.Fatal("an inline body whose digest does not match the record was replayed")
		}
	})

	t.Run("a swapped stored body is refused", func(t *testing.T) {
		snap, err := Snapshot(ctx, sink, 0, bigObject(), "p/", capturedAt)
		if err != nil {
			t.Fatalf("Snapshot: %v", err)
		}
		sink.mu.Lock()
		sink.objects[snap.ObjectRef.Key] = []byte(`{"kind":"Deployment","spec":{"replicas":0}}`)
		sink.mu.Unlock()
		if _, err := LoadSnapshot(ctx, sink, snap); err == nil {
			t.Fatal("a stored body that had been replaced was replayed; the digest is the only defence the blob sink has")
		}
	})
}

func TestSortedTargetIndices(t *testing.T) {
	// Callers check snapshot coverage against spec.targets. preState is a list the broker appends to
	// as it walks a fan-out, so its order follows execution rather than target index -- comparing
	// against a sorted target list without sorting this one first would report a gap that is not
	// there, and fail the action.
	snaps := []agentv1alpha1.PreStateSnapshot{
		{TargetIndex: 2}, {TargetIndex: 0}, {TargetIndex: 1}, {TargetIndex: 0},
	}
	got := SortedTargetIndices(snaps)
	want := []int32{0, 0, 1, 2}
	if len(got) != len(want) {
		t.Fatalf("SortedTargetIndices returned %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("SortedTargetIndices returned %v, want %v", got, want)
		}
	}
	if len(SortedTargetIndices(nil)) != 0 {
		t.Fatal("SortedTargetIndices(nil) must be empty, not a one-element slice of the zero index")
	}
}
