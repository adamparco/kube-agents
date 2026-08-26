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
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeReader answers from a map keyed by name, and records what it was asked for.
type fakeReader struct {
	objects map[string]*unstructured.Unstructured
	errs    map[string]error
	asked   []string
}

func (f *fakeReader) Get(_ context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	f.asked = append(f.asked, ref.Name)
	if err, ok := f.errs[ref.Name]; ok {
		return nil, err
	}
	obj, ok := f.objects[ref.Name]
	if !ok {
		return nil, apierrors.NewNotFound(schema.GroupResource{Resource: strings.ToLower(ref.Kind)}, ref.Name)
	}
	return obj, nil
}

type fakeStore struct {
	puts   int
	digest string // when set, the digest the store claims, to simulate a mismatch
	err    error
}

func (f *fakeStore) Put(_ context.Context, actionID string, targetIndex int, body []byte) (*agentv1alpha1.ObjectStoreRef, error) {
	f.puts++
	if f.err != nil {
		return nil, f.err
	}
	sum := sha256.Sum256(body)
	d := hex.EncodeToString(sum[:])
	if f.digest != "" {
		d = f.digest
	}
	return &agentv1alpha1.ObjectStoreRef{
		Store:  "test",
		Key:    fmt.Sprintf("%s/%d", actionID, targetIndex),
		SHA256: d,
	}, nil
}

func ref(name string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: "team-a", Name: name}
}

func configMap(name string, data map[string]any) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":            name,
			"namespace":       "team-a",
			"uid":             "uid-" + name,
			"resourceVersion": "42",
		},
		"data": data,
	}}
}

func TestCaptureAllInlinesSmallBodies(t *testing.T) {
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{
		"a": configMap("a", map[string]any{"k": "v"}),
	}}

	snaps, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{ref("a")}, metav1.Now(), nil)
	if err != nil {
		t.Fatalf("CaptureAll: %v", err)
	}
	if len(snaps) != 1 {
		t.Fatalf("snapshots = %d, want 1", len(snaps))
	}
	s := snaps[0]
	if !s.Existed {
		t.Fatal("Existed = false for an object that is there")
	}
	if s.Record == nil || s.Record.Object == nil {
		t.Fatal("a small body was not inlined")
	}
	if s.Record.ObjectRef != nil {
		t.Fatal("a small body also got a store reference; the CEL rule requires exactly one")
	}
	if len(s.Record.SHA256) != 64 {
		t.Fatalf("digest = %q, want 64 lower-hex characters", s.Record.SHA256)
	}
	if s.Live == nil {
		t.Fatal("the unsanitized live object was not retained; the undo planner needs it")
	}
}

func TestCaptureAllRepinsFromWhatWasRead(t *testing.T) {
	// The ref handed in was pinned at classification. If the object was replaced since, the
	// snapshot is of the NEW object, and the undo precondition must compare against that one.
	stale := ref("a")
	stale.UID = "uid-from-classification"
	stale.ResourceVersion = "1"

	r := &fakeReader{objects: map[string]*unstructured.Unstructured{"a": configMap("a", map[string]any{"k": "v"})}}
	snaps, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{stale}, metav1.Now(), nil)
	if err != nil {
		t.Fatalf("CaptureAll: %v", err)
	}
	if snaps[0].Ref.UID != "uid-a" {
		t.Fatalf("UID = %q, want the one observed at snapshot time", snaps[0].Ref.UID)
	}
	if snaps[0].Ref.ResourceVersion != "42" {
		t.Fatalf("resourceVersion = %q, want the one observed at snapshot time", snaps[0].Ref.ResourceVersion)
	}
}

func TestCaptureAllAbsentObjectIsNotAFailure(t *testing.T) {
	// A create's target does not exist yet, and a delete of an already-gone object has nothing to
	// do. Both must be distinguishable from "the API server would not answer".
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{}}
	snaps, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{ref("gone")}, metav1.Now(), nil)
	if err != nil {
		t.Fatalf("CaptureAll: %v", err)
	}
	if snaps[0].Existed {
		t.Fatal("Existed = true for an absent object")
	}
	if snaps[0].Record != nil {
		t.Fatal("an absent object produced a pre-state record; there is nothing to restore")
	}
	if len(Records(snaps)) != 0 {
		t.Fatal("Records included an absent object")
	}
}

func TestCaptureAllIsAllOrNothing(t *testing.T) {
	// V-BRK-018. Two targets snapshot, the third does not. The tempting behaviour -- apply the two
	// that are undoable -- produces a half-applied action whose undo restores two thirds of it.
	r := &fakeReader{
		objects: map[string]*unstructured.Unstructured{
			"a": configMap("a", map[string]any{"k": "v"}),
			"b": configMap("b", map[string]any{"k": "v"}),
		},
		errs: map[string]error{"c": fmt.Errorf("etcdserver: request timed out")},
	}

	snaps, err := CaptureAll(context.Background(), r, "act-1",
		[]agentv1alpha1.TargetRef{ref("a"), ref("b"), ref("c")}, metav1.Now(), nil)
	if err == nil {
		t.Fatal("a failed snapshot did not fail the capture")
	}
	if snaps != nil {
		t.Fatalf("CaptureAll returned %d snapshots alongside its error; a caller could apply them", len(snaps))
	}
	for _, want := range []string{"target 2", "none of the 3 targets"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not say %q: %v", want, err)
		}
	}
}

func TestCaptureAllRefusesALargeBodyWithNoStore(t *testing.T) {
	big := strings.Repeat("x", MaxInlineSnapshotBytes+1)
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{"a": configMap("a", map[string]any{"blob": big})}}

	_, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{ref("a")}, metav1.Now(), nil)
	if err == nil {
		t.Fatal("a body over the inline limit was captured with no store to hold it")
	}
	if !strings.Contains(err.Error(), "refused") {
		t.Fatalf("the error should say the action is refused: %v", err)
	}
}

func TestCaptureAllStoresALargeBody(t *testing.T) {
	big := strings.Repeat("x", MaxInlineSnapshotBytes+1)
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{"a": configMap("a", map[string]any{"blob": big})}}
	store := &fakeStore{}

	snaps, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{ref("a")}, metav1.Now(), store)
	if err != nil {
		t.Fatalf("CaptureAll: %v", err)
	}
	if store.puts != 1 {
		t.Fatalf("store.puts = %d, want 1", store.puts)
	}
	rec := snaps[0].Record
	if rec.Object != nil {
		t.Fatal("a stored body was also inlined")
	}
	if rec.ObjectRef == nil {
		t.Fatal("a stored body has no reference")
	}
	if rec.ObjectRef.SHA256 != rec.SHA256 {
		t.Fatalf("the record digest %q and the reference digest %q differ", rec.SHA256, rec.ObjectRef.SHA256)
	}
}

func TestCaptureAllRefusesAStoreDigestMismatch(t *testing.T) {
	// The store is supposed to digest what it received. A mismatch means it holds something else,
	// and the undo path would discover that at undo time, under pressure, instead of now.
	big := strings.Repeat("x", MaxInlineSnapshotBytes+1)
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{"a": configMap("a", map[string]any{"blob": big})}}
	store := &fakeStore{digest: strings.Repeat("0", 64)}

	_, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{ref("a")}, metav1.Now(), store)
	if err == nil {
		t.Fatal("a store reporting the wrong digest was accepted")
	}
}

func TestCaptureAllRedactsSecrets(t *testing.T) {
	secret := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "db", "namespace": "team-a", "uid": "uid-db", "resourceVersion": "7"},
		"data":       map[string]any{"password": "c3VwZXItc2VjcmV0"},
	}}
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{"db": secret}}
	target := agentv1alpha1.TargetRef{Version: "v1", Kind: "Secret", Namespace: "team-a", Name: "db"}

	snaps, err := CaptureAll(context.Background(), r, "act-1", []agentv1alpha1.TargetRef{target}, metav1.Now(), nil)
	if err != nil {
		t.Fatalf("CaptureAll: %v", err)
	}
	if len(snaps[0].Redactions) == 0 {
		t.Fatal("a Secret snapshot reported no redactions")
	}
	if strings.Contains(string(snaps[0].Record.Object.Raw), "c3VwZXItc2VjcmV0") {
		t.Fatal("the persisted snapshot carries the secret value")
	}
	// The in-memory copy keeps it: the undo planner has to be able to restore the real value.
	data, _, _ := unstructured.NestedStringMap(snaps[0].Live.Object, "data")
	if data["password"] != "c3VwZXItc2VjcmV0" {
		t.Fatal("the in-memory live object lost the value the undo plan needs")
	}
}

func TestCaptureAllRequiresAReaderAndAnActionID(t *testing.T) {
	if _, err := CaptureAll(context.Background(), nil, "act-1", nil, metav1.Now(), nil); err == nil {
		t.Error("a nil reader was accepted")
	}
	r := &fakeReader{objects: map[string]*unstructured.Unstructured{}}
	if _, err := CaptureAll(context.Background(), r, "", nil, metav1.Now(), nil); err == nil {
		t.Error("an empty action id was accepted")
	}
}
