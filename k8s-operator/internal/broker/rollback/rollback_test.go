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

package rollback_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	k8stypes "k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// The V-REV-011 L1 suite: a rollback replays the pre-state or refuses, and never replays a body
// that is not the pre-state.
//
// Everything here runs against fakes, on purpose, and the split from the envtest file next to it is
// not arbitrary. What this file tests is what the replayer REFUSES, and a refusal is decided before
// any request is made -- there is no API server in the story, and using one would only make the
// tests slower and the failures further from their cause. What genuinely needs a real server is the
// other half: that a create fails where an apply adopts, and that a uid precondition is enforced by
// the server rather than by us. That is rollback_envtest_test.go.
//
// The interface assertion is here rather than in the production file so that `rollback` does not
// import `verify` -- the consumer defines the interface, and the direction is worth keeping. It is
// still a compile error the moment the two drift, which is the property that matters.
var _ verify.Rollbacker = (*rollback.Replayer)(nil)

// --- fakes ---------------------------------------------------------------------------------------

type writeKind string

const (
	wroteCreate writeKind = "create"
	wroteApply  writeKind = "apply"
	wroteScale  writeKind = "scale"
	wroteDelete writeKind = "delete"
)

type write struct {
	kind     writeKind
	manager  string
	obj      *unstructured.Unstructured
	replicas int32
	opts     execute.DeleteOpts
	ref      agentv1alpha1.TargetRef
	// dryRun records the flag the caller passed. Recorded rather than ignored because the replayer
	// and the plan-time validator share this Writer and differ in exactly this bit: one of them
	// must never mutate anything, and the only way to see that from here is to look.
	dryRun bool
}

type fakeWriter struct {
	writes []write
	// errs is keyed by write kind, so a test can make exactly one leg fail.
	errs map[writeKind]error
}

func (w *fakeWriter) record(x write) error {
	w.writes = append(w.writes, x)
	return w.errs[x.kind]
}

func (w *fakeWriter) Create(_ context.Context, obj *unstructured.Unstructured, fm string, dry bool) (*unstructured.Unstructured, error) {
	return obj, w.record(write{kind: wroteCreate, manager: fm, obj: obj, dryRun: dry})
}

func (w *fakeWriter) Apply(_ context.Context, obj *unstructured.Unstructured, fm string, dry bool) (*unstructured.Unstructured, error) {
	return obj, w.record(write{kind: wroteApply, manager: fm, obj: obj, dryRun: dry})
}

func (w *fakeWriter) Scale(_ context.Context, ref agentv1alpha1.TargetRef, replicas int32, fm string, dry bool) (*unstructured.Unstructured, error) {
	return nil, w.record(write{kind: wroteScale, manager: fm, replicas: replicas, ref: ref, dryRun: dry})
}

func (w *fakeWriter) Delete(_ context.Context, ref agentv1alpha1.TargetRef, opts execute.DeleteOpts, dry bool) error {
	return w.record(write{kind: wroteDelete, opts: opts, ref: ref, dryRun: dry})
}

func (w *fakeWriter) kinds() []writeKind {
	out := make([]writeKind, 0, len(w.writes))
	for _, x := range w.writes {
		out = append(out, x.kind)
	}
	return out
}

// fakeReader answers the uid precondition. `uid` is what a Get returns; empty means NotFound.
type fakeReader struct {
	uid string
	err error
}

func (r *fakeReader) Get(_ context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	if r.err != nil {
		return nil, r.err
	}
	if r.uid == "" {
		return nil, apierrors.NewNotFound(
			schemaResource(ref), ref.Name)
	}
	obj := &unstructured.Unstructured{Object: map[string]any{}}
	obj.SetName(ref.Name)
	obj.SetNamespace(ref.Namespace)
	obj.SetUID(k8stypes.UID(r.uid))
	return obj, nil
}

type fakeSink struct {
	name string
	body []byte
	err  error
}

func (s *fakeSink) Name() string { return s.name }
func (s *fakeSink) Put(context.Context, string, []byte) (string, error) {
	return "", errors.New("the replayer never writes to the sink")
}
func (s *fakeSink) Get(context.Context, string) ([]byte, error) {
	if s.err != nil {
		return nil, s.err
	}
	return s.body, nil
}

var _ journal.BlobSink = (*fakeSink)(nil)

// --- builders ------------------------------------------------------------------------------------

const (
	testUID  = "11111111-2222-3333-4444-555555555555"
	otherUID = "99999999-8888-7777-6666-555555555555"
	identity = "platform/prod"
)

func deployRef() agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{
		Group: "apps", Version: "v1", Kind: "Deployment",
		Namespace: "team-x", Name: "api-gateway",
	}
}

func secretRef() agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: "Secret", Namespace: "team-x", Name: "db"}
}

func rawOf(t *testing.T, obj map[string]any) *runtime.RawExtension {
	t.Helper()
	u := &unstructured.Unstructured{Object: obj}
	raw, err := u.MarshalJSON()
	if err != nil {
		t.Fatalf("marshal fixture: %v", err)
	}
	return &runtime.RawExtension{Raw: raw}
}

// deployBody is a sanitized pre-state for deployRef(): no server-owned metadata, one replica field.
func deployBody(t *testing.T, replicas int64) *runtime.RawExtension {
	t.Helper()
	return rawOf(t, map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata":   map[string]any{"name": "api-gateway", "namespace": "team-x"},
		"spec":       map[string]any{"replicas": replicas},
	})
}

func planOf(steps ...agentv1alpha1.UndoStep) agentv1alpha1.UndoPlan {
	return agentv1alpha1.UndoPlan{
		Strategy:    agentv1alpha1.UndoRestore,
		GeneratedAt: metav1.Now(),
		Validated:   true,
		Steps:       steps,
	}
}

func pin(uid string) *agentv1alpha1.UndoPrecondition {
	return &agentv1alpha1.UndoPrecondition{UID: uid}
}

// replay is the whole call under test, with a live reader that agrees with the pin by default.
func replay(t *testing.T, r *rollback.Replayer, plan agentv1alpha1.UndoPlan) error {
	t.Helper()
	return r.Rollback(context.Background(), "a-1", identity, plan)
}

func mustContain(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("want a refusal mentioning %q, got no error at all", want)
	}
	if !strings.Contains(err.Error(), want) {
		t.Fatalf("refusal does not mention %q:\n  %v", want, err)
	}
}

// --- the headline: a redacted Secret is refused, not restored ------------------------------------

// The single most dangerous body this package can be handed, and the one it must never write.
//
// 06 §4.3.1 sanitizes a Secret by replacing every value with "sha256:<digest of the value>", and the
// plan's own caveat tells the reader the material "lives in the journal store and is verified
// against those digests on replay". It does not: execute.Snapshot.Live is the only unredacted copy
// and it never leaves memory. So the tempting implementation -- hydrate, apply, report success --
// does not error. It succeeds, and every consumer of the Secret gets sixty-four characters of hex
// where its credential was, from an operation whose purpose was to put things back.
//
// The refusal names the keys because the operator's next question is "which ones", and the answer
// determines whether the credential can be re-issued or has to be rotated.
func TestARedactedSecretIsRefusedRatherThanRestoredAsDigests(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	body := rawOf(t, map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "db", "namespace": "team-x"},
		"data": map[string]any{
			"password": "sha256:" + strings.Repeat("a", 64),
			"username": "cm9vdA==", // base64 "root" -- genuine material, left alone
		},
	})

	err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: secretRef(), Object: body, Preconditions: pin(testUID),
	}))

	mustContain(t, err, "REFUSING")
	mustContain(t, err, "data[password]")
	// The other key must NOT be named: reporting a real value as redacted would send an operator to
	// rotate a credential that was never lost.
	if strings.Contains(err.Error(), "data[username]") {
		t.Errorf("the refusal names a key that holds genuine material:\n  %v", err)
	}
	if len(w.writes) != 0 {
		t.Fatalf("the replayer wrote %v; a refused Secret must not reach the API server", w.kinds())
	}
}

// The negative control for the check above (09 §6, mandatory for `¬`). A Secret whose values are
// genuine base64 replays normally -- otherwise the refusal above could be "this package cannot
// restore Secrets at all", which would pass the positive test and be useless.
func TestAnUnredactedSecretReplaysNormally(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	body := rawOf(t, map[string]any{
		"apiVersion": "v1",
		"kind":       "Secret",
		"metadata":   map[string]any{"name": "db", "namespace": "team-x"},
		"data":       map[string]any{"password": "aHVudGVyMg==", "username": "cm9vdA=="},
	})

	if err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: secretRef(), Object: body, Preconditions: pin(testUID),
	})); err != nil {
		t.Fatalf("a Secret holding real material was refused: %v", err)
	}
	if got := w.kinds(); len(got) != 1 || got[0] != wroteApply {
		t.Fatalf("writes = %v, want one apply", got)
	}
}

// A ConfigMap value that merely LOOKS like a digest is not a redaction. An image digest in a
// ConfigMap is the everyday case, and a value-only matcher would refuse to restore it forever.
func TestAConfigMapHoldingADigestStringIsNotMistakenForARedaction(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	ref := agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: "team-x", Name: "images"}
	body := rawOf(t, map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata":   map[string]any{"name": "images", "namespace": "team-x"},
		"data":       map[string]any{"gateway": "sha256:" + strings.Repeat("b", 64)},
	})

	if err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: ref, Object: body, Preconditions: pin(testUID),
	})); err != nil {
		t.Fatalf("a ConfigMap holding an image digest was refused as if it were a redacted Secret: %v", err)
	}
}

// --- the plan's promises about identity ----------------------------------------------------------

// `preconditionsFor`: "the uid is what makes a restore safe to replay minutes or hours later.
// Without it... a name is reused." Three ways that can fail, all of them refusals.
func TestARestoreRefusesUnlessTheLiveObjectIsTheOneThePlanPinned(t *testing.T) {
	body := deployBody(t, 3)

	cases := []struct {
		name   string
		reader *fakeReader
		pin    *agentv1alpha1.UndoPrecondition
		want   string
	}{
		{
			name:   "no pin at all is a lookup by name",
			reader: &fakeReader{uid: testUID},
			pin:    &agentv1alpha1.UndoPrecondition{},
			want:   "pins no uid",
		},
		{
			name:   "the object was replaced under the same name",
			reader: &fakeReader{uid: otherUID},
			pin:    pin(testUID),
			want:   "was replaced after the action",
		},
		{
			// The plan chose `apply`, which asserts the object exists. Server-side apply would
			// CREATE it instead -- silently promoting a restore into a recreate, an operation the
			// planner considers separately and can refuse.
			name:   "the object is gone, so this is no longer a restore",
			reader: &fakeReader{},
			pin:    pin(testUID),
			want:   "no longer exists",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := &fakeWriter{}
			r := &rollback.Replayer{Writer: w, Reader: tc.reader}
			err := replay(t, r, planOf(agentv1alpha1.UndoStep{
				Op: "apply", Target: deployRef(), Object: body, Preconditions: tc.pin,
			}))
			mustContain(t, err, tc.want)
			if len(w.writes) != 0 {
				t.Fatalf("the replayer wrote %v after refusing", w.kinds())
			}
		})
	}
}

// The body and the target are two independent fields and nothing in the type system ties them
// together. The precondition is checked against the TARGET; the bytes that reach the API server are
// the BODY. A plan where they disagree passes every uid check and writes to the wrong object, and
// every artifact a reviewer reads afterwards names the target.
func TestABodyThatAddressesADifferentObjectThanTheTargetIsRefused(t *testing.T) {
	cases := []struct {
		name string
		obj  map[string]any
		want string
	}{
		{"a different name", map[string]any{
			"apiVersion": "apps/v1", "kind": "Deployment",
			"metadata": map[string]any{"name": "payments", "namespace": "team-x"},
		}, "named payments"},
		{"a different namespace", map[string]any{
			"apiVersion": "apps/v1", "kind": "Deployment",
			"metadata": map[string]any{"name": "api-gateway", "namespace": "team-y"},
		}, `is in "team-y"`},
		{"a different kind", map[string]any{
			"apiVersion": "apps/v1", "kind": "StatefulSet",
			"metadata": map[string]any{"name": "api-gateway", "namespace": "team-x"},
		}, "body is a StatefulSet"},
		{"a different apiVersion", map[string]any{
			"apiVersion": "apps/v1beta1", "kind": "Deployment",
			"metadata": map[string]any{"name": "api-gateway", "namespace": "team-x"},
		}, "body is apps/v1beta1"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := &fakeWriter{}
			r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}
			err := replay(t, r, planOf(agentv1alpha1.UndoStep{
				Op: "apply", Target: deployRef(), Object: rawOf(t, tc.obj), Preconditions: pin(testUID),
			}))
			mustContain(t, err, tc.want)
			if len(w.writes) != 0 {
				t.Fatalf("wrote %v after refusing", w.kinds())
			}
		})
	}
}

// A body carrying fields the API server owns did not come from Sanitize. The interesting fact is not
// what `resourceVersion` would do on replay -- it is that some other path built this step, and the
// question becomes what else that path skipped.
func TestABodyCarryingServerOwnedMetadataIsRefused(t *testing.T) {
	for _, field := range undo.DroppedMetadataFields() {
		t.Run(field, func(t *testing.T) {
			w := &fakeWriter{}
			r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}
			meta := map[string]any{"name": "api-gateway", "namespace": "team-x", field: "x"}
			err := replay(t, r, planOf(agentv1alpha1.UndoStep{
				Op: "apply", Target: deployRef(), Preconditions: pin(testUID),
				Object: rawOf(t, map[string]any{
					"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
				}),
			}))
			mustContain(t, err, "metadata."+field)
			mustContain(t, err, "did not come from the 06 §4.3.1 sanitizer")
			if len(w.writes) != 0 {
				t.Fatalf("wrote %v after refusing", w.kinds())
			}
		})
	}
}

// The negative control for the two checks above: the fixture they mutate replays cleanly. Without
// this, "everything is refused" would satisfy both.
func TestASanitizedBodyMatchingItsTargetReplays(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}
	if err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
	})); err != nil {
		t.Fatalf("the clean fixture was refused: %v", err)
	}
	if got := w.kinds(); len(got) != 1 || got[0] != wroteApply {
		t.Fatalf("writes = %v, want one apply", got)
	}
}

// --- out-of-band bodies --------------------------------------------------------------------------

// journal.LoadSnapshot re-verifies the digest on every read and this must too, for the same reason:
// the bytes travelled through a store this process does not own. A mismatch is a body that is not
// the pre-state, which is exactly what V-REV-011 forbids replaying.
func TestAnOutOfBandBodyIsVerifiedAgainstTheDigestThePlanRecorded(t *testing.T) {
	good := deployBody(t, 3).Raw
	digest := journal.Digest(good)

	step := func(sha string) agentv1alpha1.UndoStep {
		return agentv1alpha1.UndoStep{
			Op: "apply", Target: deployRef(), Preconditions: pin(testUID),
			ObjectRef: &agentv1alpha1.ObjectStoreRef{Store: "journal", Key: "k", SHA256: sha},
		}
	}

	t.Run("a body that matches its digest replays", func(t *testing.T) {
		w := &fakeWriter{}
		r := &rollback.Replayer{
			Writer: w, Reader: &fakeReader{uid: testUID},
			Sink: &fakeSink{name: "journal", body: good},
		}
		if err := replay(t, r, planOf(step(digest))); err != nil {
			t.Fatalf("a body matching its digest was refused: %v", err)
		}
	})

	t.Run("a body that changed in the store is refused", func(t *testing.T) {
		w := &fakeWriter{}
		r := &rollback.Replayer{
			Writer: w, Reader: &fakeReader{uid: testUID},
			Sink: &fakeSink{name: "journal", body: deployBody(t, 99).Raw},
		}
		mustContain(t, replay(t, r, planOf(step(digest))), "refusing to replay a body that changed")
		if len(w.writes) != 0 {
			t.Fatalf("wrote %v after a digest mismatch", w.kinds())
		}
	})

	t.Run("a store this broker is not configured for is named, not guessed at", func(t *testing.T) {
		r := &rollback.Replayer{
			Writer: &fakeWriter{}, Reader: &fakeReader{uid: testUID},
			Sink: &fakeSink{name: "somewhere-else", body: good},
		}
		mustContain(t, replay(t, r, planOf(step(digest))), `but the configured sink is "somewhere-else"`)
	})

	t.Run("no sink configured is a refusal, not a nil dereference", func(t *testing.T) {
		r := &rollback.Replayer{Writer: &fakeWriter{}, Reader: &fakeReader{uid: testUID}}
		mustContain(t, replay(t, r, planOf(step(digest))), "no blob sink configured")
	})
}

// --- per-op behaviour ----------------------------------------------------------------------------

// A recreate must be a genuine create. The planner's comment is explicit that this is the ONLY thing
// standing in for the uid pin it cannot have: "`create` fails if something already holds the name".
// An apply at the same name adopts. This test watches which method was called; the envtest file
// proves the two really do differ against a real server.
func TestARecreateCallsCreateAndNeverApply(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	if err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "create", Target: deployRef(), Object: deployBody(t, 3),
	})); err != nil {
		t.Fatalf("recreate: %v", err)
	}
	if got := w.kinds(); len(got) != 1 || got[0] != wroteCreate {
		t.Fatalf("writes = %v, want exactly one create -- an apply here adopts a stranger at the same name", got)
	}
}

// AlreadyExists is the case the whole design of the recreate step turns on, so the message has to
// say what happened rather than surface a bare API error.
func TestARecreateOntoATakenNameRefusesAndSaysWhy(t *testing.T) {
	w := &fakeWriter{errs: map[writeKind]error{
		wroteCreate: apierrors.NewAlreadyExists(schemaResource(deployRef()), "api-gateway"),
	}}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "create", Target: deployRef(), Object: deployBody(t, 3),
	}))
	mustContain(t, err, "something already holds that name")
	mustContain(t, err, "refusing rather than overwriting it")
}

// A delete whose object is already gone has achieved its goal. Erroring would climb rung 5, page a
// human and pause the agent over a satisfied post-condition -- and the race is ordinary: a rollback
// competes with the garbage collector whenever the created object had an owner the same failure
// destroyed.
func TestADeleteTreatsAnAlreadyAbsentObjectAsSuccess(t *testing.T) {
	w := &fakeWriter{errs: map[writeKind]error{
		wroteDelete: apierrors.NewNotFound(schemaResource(deployRef()), "api-gateway"),
	}}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	if err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "delete", Target: deployRef(), Preconditions: pin(testUID),
	})); err != nil {
		t.Fatalf("an already-absent object made the rollback fail: %v", err)
	}
	if got := w.writes; len(got) != 1 || got[0].opts.UID != testUID {
		t.Fatalf("the delete did not carry the uid precondition: %+v", got)
	}
}

// Any OTHER delete error is a real failure. Without this, "NotFound is success" could be
// implemented as "every delete error is success".
func TestADeleteThatFailsForAnyOtherReasonIsAFailure(t *testing.T) {
	w := &fakeWriter{errs: map[writeKind]error{wroteDelete: errors.New("conflict: uid mismatch")}}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	mustContain(t, replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "delete", Target: deployRef(), Preconditions: pin(testUID),
	})), "uid mismatch")
}

// A scale restores one field through the scale subresource, not the whole snapshot. Applying the
// body would additionally revert every field another manager has legitimately changed since -- a
// rollback with a blast radius larger than the action it reverses.
func TestAScaleRestoresTheReplicaCountAndNothingElse(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	if err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "scale", Target: deployRef(), Object: deployBody(t, 4), Preconditions: pin(testUID),
	})); err != nil {
		t.Fatalf("scale: %v", err)
	}
	if got := w.kinds(); len(got) != 1 || got[0] != wroteScale {
		t.Fatalf("writes = %v, want exactly one scale", got)
	}
	if got := w.writes[0].replicas; got != 4 {
		t.Fatalf("scaled to %d, want the snapshot's 4", got)
	}
}

// A scale step whose body has no replica count cannot restore anything, and the honest answer is to
// say the number is missing rather than to default it. Defaulting to zero would scale the workload
// down as an "undo".
func TestAScaleWithNoReplicaCountInTheSnapshotRefuses(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	body := rawOf(t, map[string]any{
		"apiVersion": "apps/v1", "kind": "Deployment",
		"metadata": map[string]any{"name": "api-gateway", "namespace": "team-x"},
		"spec":     map[string]any{"paused": true},
	})
	mustContain(t, replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "scale", Target: deployRef(), Object: body, Preconditions: pin(testUID),
	})), "carries no spec.replicas")
	if len(w.writes) != 0 {
		t.Fatalf("wrote %v rather than refusing", w.kinds())
	}
}

// --- ordering, atomicity, and the manager string --------------------------------------------------

// A plan is an ordered sequence chosen to restore one coherent state. Stopping at the first failure
// is the same rule as V-BRK-018's snapshot atomicity arriving at the other end of the action, and
// the count of already-applied steps is in the message because it is the difference between
// "nothing happened" and "the world is in a state nobody asked for".
func TestAFailedStepStopsTheReplayAndReportsWhatHadAlreadyBeenApplied(t *testing.T) {
	w := &fakeWriter{errs: map[writeKind]error{wroteScale: errors.New("boom")}}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	err := replay(t, r, planOf(
		agentv1alpha1.UndoStep{Op: "apply", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID)},
		agentv1alpha1.UndoStep{Op: "scale", Target: deployRef(), Object: deployBody(t, 4), Preconditions: pin(testUID)},
		agentv1alpha1.UndoStep{Op: "delete", Target: deployRef(), Preconditions: pin(testUID)},
	))

	mustContain(t, err, "stopped at step 2 of 3")
	mustContain(t, err, "1 step(s) already replayed")
	mustContain(t, err, "they are NOT reverted")
	if got := w.kinds(); len(got) != 2 {
		t.Fatalf("writes = %v, want the replay to stop after the failing step", got)
	}
}

// The field manager comes from the per-call identity (V-BRK-019), not from a value bound into the
// Replayer at construction. A statically-bound manager is right until the process serves a second
// agent and silently wrong thereafter, attributing one agent's rollback to another in managedFields.
func TestTheFieldManagerNamesTheAgentPassedToThisCall(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	for _, id := range []string{"platform/prod", "developer-team/team-x"} {
		if err := r.Rollback(context.Background(), "a-1", id, planOf(agentv1alpha1.UndoStep{
			Op: "apply", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
		})); err != nil {
			t.Fatalf("replay as %s: %v", id, err)
		}
		want, err := execute.FieldManager(id)
		if err != nil {
			t.Fatalf("FieldManager(%q): %v", id, err)
		}
		if got := w.writes[len(w.writes)-1].manager; got != want {
			t.Fatalf("manager = %q, want %q", got, want)
		}
	}
}

// An identity that cannot produce a manager is refused before anything is written. execute's own
// rule: "a wrong one is invisible until something downstream compares it, which is exactly when the
// comparison matters."
func TestAnIdentityThatCannotProduceAFieldManagerIsRefusedBeforeAnyWrite(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	err := r.Rollback(context.Background(), "a-1", "", planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
	}))
	mustContain(t, err, "the agent identity is empty")
	if len(w.writes) != 0 {
		t.Fatalf("wrote %v with no usable field manager", w.kinds())
	}
}

// --- the seam with the validator ------------------------------------------------------------------

// undo.ReplayableOps is the single definition site for the op set, and this is what makes it one.
// Before this package existed, ValidateReplayable's default arm asserted that an unknown op was one
// "which the replayer does not implement" -- a claim about a component that had not been written.
// Now both ends read the same list, and an op added to one and not the other fails here. LSN-040.
func TestTheReplayerImplementsEveryReplayableOp(t *testing.T) {
	for _, op := range undo.ReplayableOps() {
		t.Run(op, func(t *testing.T) {
			w := &fakeWriter{}
			r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}
			step := agentv1alpha1.UndoStep{
				Op: op, Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
			}
			if err := replay(t, r, planOf(step)); err != nil {
				t.Fatalf("op %q is in undo.ReplayableOps() but the replayer refused it: %v", op, err)
			}
			if len(w.writes) != 1 {
				t.Fatalf("op %q produced %v; every replayable op must reach the API server", op, w.kinds())
			}
		})
	}
}

// The other direction. An op outside the set is refused BY THE VALIDATOR, before dispatch -- so the
// replayer's own default arm is unreachable through this entry point, which is the property that
// makes the single definition site real rather than decorative.
func TestAnOpOutsideTheReplayableSetIsRefusedByName(t *testing.T) {
	w := &fakeWriter{}
	r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}

	err := replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "evacuate", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
	}))
	mustContain(t, err, `op "evacuate"`)
	mustContain(t, err, "not one of the replayable ops")
	if len(w.writes) != 0 {
		t.Fatalf("wrote %v for an op nothing implements", w.kinds())
	}
}

// The validator runs here and not only in verify.Driver, because the OTHER caller is the undo
// controller's replay path, which arrives from a human command rather than from the driver. A
// validator on one of two paths is one that gets discovered missing on the other during an incident.
func TestAPlanTheValidatorRejectsIsNeverReplayed(t *testing.T) {
	cases := []struct {
		name string
		plan agentv1alpha1.UndoPlan
		want string
	}{
		{
			name: "strategy none was recorded as not undoable",
			plan: agentv1alpha1.UndoPlan{Strategy: agentv1alpha1.UndoNone, Validated: true},
			want: "must not be replayed",
		},
		{
			name: "never dry-run, so nothing checked that its steps would apply",
			plan: agentv1alpha1.UndoPlan{
				Strategy: agentv1alpha1.UndoRestore,
				Steps: []agentv1alpha1.UndoStep{{
					Op: "apply", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
				}},
			},
			want: "never dry-run",
		},
		{
			name: "a delete with no uid pin",
			plan: planOf(agentv1alpha1.UndoStep{Op: "delete", Target: deployRef()}),
			want: "no uid precondition",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := &fakeWriter{}
			r := &rollback.Replayer{Writer: w, Reader: &fakeReader{uid: testUID}}
			err := replay(t, r, tc.plan)
			mustContain(t, err, "not replayable")
			mustContain(t, err, tc.want)
			if len(w.writes) != 0 {
				t.Fatalf("wrote %v for a plan the validator rejected", w.kinds())
			}
		})
	}
}

// A Replayer with no Reader cannot check a single precondition in this package, so it must refuse
// rather than degrade into the name-lookup the pins exist to prevent. Failing closed on a missing
// collaborator is the same rule the prober follows for an unobservable property.
func TestAReplayerWithNoReaderRefusesRatherThanSkippingPreconditions(t *testing.T) {
	r := &rollback.Replayer{Writer: &fakeWriter{}}
	mustContain(t, replay(t, r, planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: deployRef(), Object: deployBody(t, 3), Preconditions: pin(testUID),
	})), "no Reader is configured")
}

// A step with neither body nor ref reaches the replayer only for ops the validator does not require
// a body for -- but the message still has to name the step rather than panic on a nil.
func TestAStepWithNoBodyAtAllIsNamedRatherThanPanicking(t *testing.T) {
	r := &rollback.Replayer{Writer: &fakeWriter{}, Reader: &fakeReader{uid: testUID}}
	plan := planOf(agentv1alpha1.UndoStep{
		Op: "apply", Target: deployRef(), Preconditions: pin(testUID),
		Object: &runtime.RawExtension{},
	})
	// ValidateReplayable accepts this: Object is non-nil, just empty.
	mustContain(t, replay(t, r, plan), "neither an inline body nor an objectRef")
}

// --- small helpers -------------------------------------------------------------------------------

// schemaResource builds the GroupResource an apierrors constructor needs. The naive pluralization is
// fine: nothing here reads the resource name back, it only has to be present so that IsNotFound and
// IsAlreadyExists behave as they would against a real server.
func schemaResource(ref agentv1alpha1.TargetRef) schema.GroupResource {
	return schema.GroupResource{Group: ref.Group, Resource: strings.ToLower(ref.Kind) + "s"}
}
