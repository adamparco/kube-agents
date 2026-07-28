package bodystore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// sinkStub is a journal.BlobSink whose every answer is dictated by the test. It is not a working
// sink and does not try to be: the properties this file asserts are about what the adapter does with
// what a sink SAYS, including the answers a correct sink would never give.
type sinkStub struct {
	name   string
	digest func(body []byte) string
	err    error

	keys   []string
	bodies [][]byte
}

func (s *sinkStub) Name() string { return s.name }

func (s *sinkStub) Put(_ context.Context, key string, body []byte) (string, error) {
	s.keys = append(s.keys, key)
	s.bodies = append(s.bodies, body)
	if s.err != nil {
		return "", s.err
	}
	if s.digest == nil {
		return journal.Digest(body), nil
	}
	return s.digest(body), nil
}

func (s *sinkStub) Get(context.Context, string) ([]byte, error) {
	return nil, errors.New("this test never reads back")
}

func honest(name string) *sinkStub { return &sinkStub{name: name} }

// ---------------------------------------------------------------------------------------------
// The happy path, and the key layout that makes an undo findable
// ---------------------------------------------------------------------------------------------

func TestPutStoresTheBodyAndReturnsAReferenceToIt(t *testing.T) {
	sink := honest("gs://kage-journal")
	j := &Journal{Sink: sink, AgentIdentity: "platform-agent"}
	body := []byte(`{"kind":"ConfigMap"}`)

	ref, err := j.Put(context.Background(), "act-42", 3, body)
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	if ref.Store != "gs://kage-journal" {
		t.Fatalf("the reference must name the sink a reader has to go to; got %q", ref.Store)
	}
	if ref.SHA256 != journal.Digest(body) {
		t.Fatalf("digest %q does not match the body", ref.SHA256)
	}
	if len(sink.bodies) != 1 || string(sink.bodies[0]) != string(body) {
		t.Fatalf("the sink received %q, not the body it was handed", sink.bodies)
	}
}

// The write key and the read key are the same string produced by two different packages. This asserts
// the adapter goes through journal's own key function rather than re-deriving the layout, because a
// divergence between the two does not fail at write time or at review time -- it fails at replay,
// months later, as a snapshot the record swears exists and the sink has never heard of.
func TestTheKeyIsJournalsAndNotAPrivateFormatString(t *testing.T) {
	sink := honest("bucket")
	j := &Journal{Sink: sink, AgentIdentity: "platform-agent"}

	ref, err := j.Put(context.Background(), "act-42", 3, []byte("x"))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	want := journal.SnapshotKey(journal.SnapshotKeyPrefix("platform-agent", "act-42"), 3)
	if ref.Key != want {
		t.Fatalf("recorded key %q, want %q", ref.Key, want)
	}
	if sink.keys[0] != want {
		t.Fatalf("the body was stored under %q but the record says %q; the undo would look in the wrong place", sink.keys[0], want)
	}
	// Spelled out once, so a change to journal's layout that this test would otherwise follow
	// silently has to be made deliberately.
	if want != "platform-agent/act-42/3" {
		t.Fatalf("the layout is identity/action/target; got %q", want)
	}
}

// Two targets of one action are two distinct keys. A layout that collapsed them would have the
// second body silently overwrite the first, and the record for target 0 would point at target 1's
// object -- an undo that restores the wrong thing while reporting success.
func TestEachTargetGetsItsOwnKey(t *testing.T) {
	sink := honest("bucket")
	j := &Journal{Sink: sink, AgentIdentity: "platform-agent"}

	seen := map[string]bool{}
	for i := 0; i < 3; i++ {
		ref, err := j.Put(context.Background(), "act-42", i, []byte(fmt.Sprintf("body-%d", i)))
		if err != nil {
			t.Fatalf("Put target %d: %v", i, err)
		}
		if seen[ref.Key] {
			t.Fatalf("target %d reused key %q", i, ref.Key)
		}
		seen[ref.Key] = true
	}
}

// Identity is part of the key, not decoration. Two agents running the same action id must not be able
// to file bodies over each other -- which is a cross-tenant collision that would look, in review, like
// a formatting slip.
func TestIdentityKeepsTwoAgentsApart(t *testing.T) {
	a := &Journal{Sink: honest("bucket"), AgentIdentity: "platform-agent"}
	b := &Journal{Sink: honest("bucket"), AgentIdentity: "developer-team"}

	refA, err := a.Put(context.Background(), "act-42", 0, []byte("x"))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	refB, err := b.Put(context.Background(), "act-42", 0, []byte("x"))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	if refA.Key == refB.Key {
		t.Fatalf("both agents filed under %q", refA.Key)
	}
}

// ---------------------------------------------------------------------------------------------
// The digest has independent provenance -- LSN-034
// ---------------------------------------------------------------------------------------------

// The adapter must return what the SINK reported, not a locally recomputed hash of the bytes it was
// handed. execute.capture compares the returned digest against its own; if this adapter recomputed
// it, that comparison would be a value compared against itself and would never fail. A sink that
// stores the wrong thing would then reach the record, and the mismatch would surface at undo time,
// under pressure, as a refusal to replay.
func TestTheReturnedDigestIsTheSinksAndIsNotRecomputed(t *testing.T) {
	const lie = "0000000000000000000000000000000000000000000000000000000000000000"
	sink := &sinkStub{name: "bucket", digest: func([]byte) string { return lie }}
	j := &Journal{Sink: sink, AgentIdentity: "platform-agent"}

	ref, err := j.Put(context.Background(), "act-42", 0, []byte("real body"))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}
	if ref.SHA256 != lie {
		t.Fatalf("the adapter substituted its own digest %q for the sink's %q; the caller's comparison would be against itself", ref.SHA256, lie)
	}
}

// The half of the property that only shows up one layer up: the disagreement the adapter passes
// through must actually stop the action. Asserted through the real execute.capture rather than by
// reasoning about it, because "the caller compares these" is exactly the kind of claim that stays
// true right up until the caller stops.
func TestASinkThatStoredSomethingElseRefusesTheAction(t *testing.T) {
	body, obj := oversizedConfigMap(t)

	honestJ := &Journal{Sink: honest("bucket"), AgentIdentity: "platform-agent"}
	lying := &Journal{
		Sink:          &sinkStub{name: "bucket", digest: func(b []byte) string { return flipOneByte(b) }},
		AgentIdentity: "platform-agent",
	}

	// Control first: the same oversized body, an honest sink, and the capture succeeds with an
	// objectRef rather than an inline body. Without this, the refusal below could be the size check
	// or the sanitizer rather than the digest comparison.
	snaps, err := execute.CaptureAll(context.Background(), readerOf(obj), "act-42",
		[]agentv1alpha1.TargetRef{targetOf(obj)}, metav1.Now(), honestJ)
	if err != nil {
		t.Fatalf("an oversized body with an honest sink must be captured: %v", err)
	}
	if snaps[0].Record.ObjectRef == nil {
		t.Fatalf("a %d-byte body should have gone out of band, not inline", len(body))
	}
	if snaps[0].Record.Object != nil {
		t.Fatal("an out-of-band snapshot must not also carry the body inline")
	}

	_, err = execute.CaptureAll(context.Background(), readerOf(obj), "act-42",
		[]agentv1alpha1.TargetRef{targetOf(obj)}, metav1.Now(), lying)
	if err == nil {
		t.Fatal("a sink that reported a digest for bytes it did not store must refuse the action")
	}
	if !strings.Contains(err.Error(), "reports digest") {
		t.Fatalf("the refusal should be the digest comparison, not something incidental: %v", err)
	}
}

// ---------------------------------------------------------------------------------------------
// Every refusal, in isolation
// ---------------------------------------------------------------------------------------------

// None of these truncates, records a partial reference, or returns a usable ref alongside its error.
// 03 §6 applied to the snapshot: an action executed without a recoverable pre-state is an action that
// cannot be undone, on a record that claims it can.
func TestEveryRefusalReturnsNoReference(t *testing.T) {
	cases := []struct {
		name        string
		j           *Journal
		actionID    string
		targetIndex int
		body        []byte
		want        string
	}{
		{
			name: "no sink behind the store",
			j:    &Journal{AgentIdentity: "platform-agent"},
			want: "no blob sink is configured",
		},
		{
			name: "no agent identity",
			j:    &Journal{Sink: honest("bucket")},
			want: "no agent identity",
		},
		{
			name:     "no action id",
			j:        &Journal{Sink: honest("bucket"), AgentIdentity: "platform-agent"},
			actionID: "",
			want:     "no action id",
		},
		{
			name:        "a negative target index",
			j:           &Journal{Sink: honest("bucket"), AgentIdentity: "platform-agent"},
			actionID:    "act-42",
			targetIndex: -1,
			want:        "is negative",
		},
		{
			name:     "an empty body",
			j:        &Journal{Sink: honest("bucket"), AgentIdentity: "platform-agent"},
			actionID: "act-42",
			body:     []byte{},
			want:     "the body is empty",
		},
		{
			name:     "the sink failed",
			j:        &Journal{Sink: &sinkStub{name: "bucket", err: errors.New("503 backend unavailable")}, AgentIdentity: "platform-agent"},
			actionID: "act-42",
			body:     []byte("x"),
			want:     "503 backend unavailable",
		},
		{
			name:     "the sink reported no digest",
			j:        &Journal{Sink: &sinkStub{name: "bucket", digest: func([]byte) string { return "" }}, AgentIdentity: "platform-agent"},
			actionID: "act-42",
			body:     []byte("x"),
			want:     "reported no digest",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			body := tc.body
			if body == nil {
				body = []byte("x")
			}
			ref, err := tc.j.Put(context.Background(), tc.actionID, tc.targetIndex, body)
			if err == nil {
				t.Fatalf("want a refusal, got %+v", ref)
			}
			if ref != nil {
				t.Fatalf("a refusal must carry no reference; got %+v", ref)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not explain itself with %q", err, tc.want)
			}
		})
	}
}

// The negative control for the table above: the same call with nothing wrong succeeds. A Put that
// refused unconditionally would pass all seven cases and make every oversized snapshot fail.
func TestAWellFormedPutSucceeds(t *testing.T) {
	j := &Journal{Sink: honest("bucket"), AgentIdentity: "platform-agent"}
	if _, err := j.Put(context.Background(), "act-42", 0, []byte("x")); err != nil {
		t.Fatalf("a well-formed Put must succeed: %v", err)
	}
}

// A sink outage costs availability for over-limit snapshots and never costs undoability. Asserted at
// the CaptureAll boundary because that is where the whole-envelope refusal lives: one target that
// could not be snapshotted stops all of them.
func TestASinkOutageStopsTheWholeEnvelope(t *testing.T) {
	_, obj := oversizedConfigMap(t)
	j := &Journal{Sink: &sinkStub{name: "bucket", err: errors.New("503")}, AgentIdentity: "platform-agent"}

	_, err := execute.CaptureAll(context.Background(), readerOf(obj), "act-42",
		[]agentv1alpha1.TargetRef{targetOf(obj)}, metav1.Now(), j)
	if err == nil {
		t.Fatal("a body that could not be persisted must refuse the action (03 §6)")
	}
	if !strings.Contains(err.Error(), "none of the") {
		t.Fatalf("the refusal should be envelope-wide: %v", err)
	}
}

// ---------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------

// oversizedConfigMap returns an object whose sanitized body clears execute.MaxInlineSnapshotBytes, so
// the out-of-band path is the one under test rather than the inline one.
func oversizedConfigMap(t *testing.T) ([]byte, *unstructured.Unstructured) {
	t.Helper()
	obj := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":            "big",
			"namespace":       "team-a",
			"uid":             "cfg-uid",
			"resourceVersion": "1",
		},
		"data": map[string]any{"blob": strings.Repeat("a", execute.MaxInlineSnapshotBytes+1024)},
	}}
	return []byte(strings.Repeat("a", execute.MaxInlineSnapshotBytes+1024)), obj
}

func targetOf(obj *unstructured.Unstructured) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{
		Version:   "v1",
		Kind:      obj.GetKind(),
		Namespace: obj.GetNamespace(),
		Name:      obj.GetName(),
		UID:       string(obj.GetUID()),
	}
}

type staticReader struct{ obj *unstructured.Unstructured }

func (r staticReader) Get(context.Context, agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	return r.obj, nil
}

func readerOf(obj *unstructured.Unstructured) execute.Reader { return staticReader{obj: obj} }

// flipOneByte digests a body that is one byte different from the one it was handed: a sink that
// stored something else and reported an honest digest OF THAT. Distinct from returning a constant --
// the digest is well formed and would pass any shape check, which is the case the comparison exists
// for.
func flipOneByte(body []byte) string {
	altered := make([]byte, len(body))
	copy(altered, body)
	if len(altered) > 0 {
		altered[0] ^= 0xff
	}
	sum := sha256.Sum256(altered)
	return hex.EncodeToString(sum[:])
}
