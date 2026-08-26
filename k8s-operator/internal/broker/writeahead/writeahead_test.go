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

package writeahead

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

const (
	testID = "01JZQ8X9K7M4N2P6R8T0V3W5YZ"
	testNS = "team-x"
)

// fakeReader answers whatever the test puts in it. Note what it does NOT do: it does not mint a UID
// or a resourceVersion. That is the point of the fixtures below spelling both out by hand -- the
// confirmer's central claim is about values only an API server produces, so a double that produced
// them for free would make every test here agree with a confirmer that had no such check. The claim
// that a REAL server supplies them is in writeahead_envtest_test.go, which is the only place it can
// honestly be made.
type fakeReader struct {
	ar   *agentv1alpha1.ActionRecord
	err  error
	ns   string // captured
	id   string // captured
	call int
}

func (f *fakeReader) Get(_ context.Context, namespace, actionID string) (*agentv1alpha1.ActionRecord, error) {
	f.call++
	f.ns, f.id = namespace, actionID
	return f.ar, f.err
}

// durable is a record shaped the way a real server hands one back: server-assigned identity, the
// action id in spec, and the phase in BOTH places journal.Store.Create leaves it -- `status.phase`,
// which 06 §4.3 makes authoritative, and the metadata label, which is its index. The fixture
// carries both because a real Create now produces both; TestCreateWritesBothThePhaseAndItsLabel is
// where that is measured against an API server rather than asserted here.
func durable(opts ...func(*agentv1alpha1.ActionRecord)) *agentv1alpha1.ActionRecord {
	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:            journal.RecordName(testID),
			Namespace:       testNS,
			UID:             "8f14e45f-ea8f-4b6a-9c1e-2d3f4a5b6c7d",
			ResourceVersion: "412",
			Labels:          map[string]string{journal.StatusLabel: string(agentv1alpha1.PhaseExecuting)},
		},
		Spec: agentv1alpha1.ActionRecordSpec{ActionID: testID},
	}
	ar.Status.Phase = agentv1alpha1.PhaseExecuting
	for _, o := range opts {
		o(ar)
	}
	return ar
}

// atPhase sets BOTH copies, which is what an agreed phase means. Refusal cases that want the phase
// arm rather than the divergence arm go through this, so that the two arms can never cover for each
// other by accident.
func atPhase(p agentv1alpha1.ActionPhase) func(*agentv1alpha1.ActionRecord) {
	return func(a *agentv1alpha1.ActionRecord) {
		a.Status.Phase = p
		if p == "" {
			delete(a.Labels, journal.StatusLabel)
			return
		}
		if a.Labels == nil {
			a.Labels = map[string]string{}
		}
		a.Labels[journal.StatusLabel] = string(p)
	}
}

func confirmer(ar *agentv1alpha1.ActionRecord, err error) (*Confirmer, *fakeReader) {
	r := &fakeReader{ar: ar, err: err}
	return &Confirmer{Records: r, Namespace: testNS}, r
}

func notFound() error {
	return apierrors.NewNotFound(schema.GroupResource{Group: agentv1alpha1.GroupVersion.Group, Resource: "actionrecords"}, journal.RecordName(testID))
}

// --- the happy path -----------------------------------------------------------------------------

func TestADurableRecordConfirms(t *testing.T) {
	c, r := confirmer(durable(), nil)
	if err := c.ConfirmDurable(context.Background(), testID); err != nil {
		t.Fatalf("a record with server identity, the right action id and phase Executing should confirm; got %v", err)
	}
	if r.call != 1 {
		t.Errorf("the confirmation must be a read: want exactly 1 Get, got %d", r.call)
	}
	if r.ns != testNS || r.id != testID {
		t.Errorf("read the wrong key: got (%q, %q), want (%q, %q)", r.ns, r.id, testNS, testID)
	}
}

// A record with no phase at all is not an error: journal.Labels omits the label when the phase is
// unset and Create returns before the status write, so BOTH copies empty is a shape a caller can
// legitimately produce. Only a NAMED and different phase is a refusal -- and only when the two
// copies agree on it, which is the case below this one.
func TestARecordWithNoPhaseAtAllConfirms(t *testing.T) {
	c, _ := confirmer(durable(atPhase("")), nil)
	if err := c.ConfirmDurable(context.Background(), testID); err != nil {
		t.Fatalf("neither copy carrying a phase is not a phase mismatch; got %v", err)
	}
}

func TestANilLabelMapWithNoPhaseConfirms(t *testing.T) {
	ar := durable(atPhase(""), func(a *agentv1alpha1.ActionRecord) { a.Labels = nil })
	c, _ := confirmer(ar, nil)
	if err := c.ConfirmDurable(context.Background(), testID); err != nil {
		t.Fatalf("a record with no labels at all must not panic or refuse; got %v", err)
	}
}

// Every phase name must survive journal.Labels unchanged, or the divergence arm refuses records
// that are perfectly consistent. journal.labelValue rewrites anything outside [A-Za-z0-9._-] and
// truncates at 63 bytes; the arm compares the label byte-for-byte against string(status.phase), so
// a phase added later with a slash or a space in it would make the two copies differ by
// construction and take every action of that phase down the divergence path. Asserted here rather
// than trusted, because the failure is total, silent at compile time, and lands on whichever phase
// somebody adds next.
func TestEveryPhaseSurvivesLabelEncodingUnchanged(t *testing.T) {
	for _, p := range agentv1alpha1.AllActionPhases() {
		ar := &agentv1alpha1.ActionRecord{}
		ar.Status.Phase = p
		got := journal.Labels(ar)[journal.StatusLabel]
		if got != string(p) {
			t.Errorf("phase %q encodes to label %q; the confirmer's divergence arm compares them byte-for-byte and would refuse every record in this phase", p, got)
		}
	}
}

// --- every refusal ------------------------------------------------------------------------------

func TestEveryRefusalIsNotDurable(t *testing.T) {
	cases := []struct {
		name string
		ar   *agentv1alpha1.ActionRecord
		err  error
		// want is a fragment the message must contain, chosen so that two different refusals can
		// never satisfy each other's assertion. A test that only checked "some error" would pass
		// against a confirmer that refused everything for one reason.
		want string
	}{
		{
			name: "the record was never written",
			err:  notFound(),
			want: "the write-ahead write never landed",
		},
		{
			name: "the read failed, so durability is unknown",
			err:  errors.New("etcdserver: request timed out"),
			want: "durability is unknown",
		},
		{
			name: "the reader answered with neither a record nor an error",
			want: "returned no record and no error",
		},
		{
			name: "no uid: nothing admitted it to storage",
			ar:   durable(func(a *agentv1alpha1.ActionRecord) { a.UID = "" }),
			want: "assigned by the API server",
		},
		{
			name: "no resourceVersion: same claim, other half",
			ar:   durable(func(a *agentv1alpha1.ActionRecord) { a.ResourceVersion = "" }),
			want: "assigned by the API server",
		},
		{
			name: "the record is being deleted",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				d := metav1.NewTime(time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC))
				a.DeletionTimestamp = &d
			}),
			want: "would outlive its journal entry",
		},
		{
			name: "the derived name and the content disagree",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Spec.ActionID = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
			}),
			want: "some other action's journal entry",
		},
		{
			name: "the durable record is parked for approval",
			ar:   durable(atPhase(agentv1alpha1.PhasePendingApproval)),
			want: `is in phase "PendingApproval"`,
		},
		{
			name: "the durable record was already rejected",
			ar:   durable(atPhase(agentv1alpha1.PhaseRejected)),
			want: `is in phase "Rejected"`,
		},
		{
			name: "the durable record is already finished",
			ar:   durable(atPhase(agentv1alpha1.PhaseVerified)),
			want: `is in phase "Verified"`,
		},

		// The divergence arm. Every one of these was ADMITTED before 2026-07-30, because the arm
		// read the label alone and the label is the copy 06 §4.3 calls derived. The first is the
		// SetPhase window reproduced by hand -- status has moved on, the label write has not landed
		// yet or was lost -- and it is the fail-open hole this arm closes.
		{
			name: "status has moved to Rejected and the label still says Executing",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Status.Phase = agentv1alpha1.PhaseRejected
			}),
			want: `status.phase "Rejected" and the kube-agents/status label "Executing"`,
		},
		{
			name: "the label has moved and status has not",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Labels[journal.StatusLabel] = string(agentv1alpha1.PhaseVerified)
			}),
			want: `status.phase "Executing" and the kube-agents/status label "Verified"`,
		},
		{
			name: "the authoritative copy is empty and the index names a phase",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Status.Phase = ""
			}),
			want: `status.phase "" and the kube-agents/status label "Executing"`,
		},
		{
			name: "the index is missing and the authoritative copy names a phase",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				delete(a.Labels, journal.StatusLabel)
			}),
			want: `status.phase "Executing" and the kube-agents/status label ""`,
		},
		{
			name: "no labels at all, and status names a phase",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Labels = nil
			}),
			want: `status.phase "Executing" and the kube-agents/status label ""`,
		},
		{
			name: "both copies name a phase and they are different non-Executing phases",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Status.Phase = agentv1alpha1.PhasePendingApproval
				a.Labels[journal.StatusLabel] = string(agentv1alpha1.PhaseRejected)
			}),
			want: `status.phase "PendingApproval" and the kube-agents/status label "Rejected"`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := confirmer(tc.ar, tc.err)
			err := c.ConfirmDurable(context.Background(), testID)
			if err == nil {
				t.Fatalf("want a refusal, got nil")
			}
			if !errors.Is(err, ErrNotDurable) {
				t.Errorf("every refusal must wrap ErrNotDurable so a caller can tell them from a transport error; got %v", err)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("the refusal must name what it found.\n got: %v\nwant substring: %q", err, tc.want)
			}
		})
	}
}

// The NotFound arm and the read-failure arm are different operator problems -- a caller that
// executed out of order versus a sick API server -- so they must not collapse into one message.
func TestTheTwoReadFailuresStaySeparate(t *testing.T) {
	c1, _ := confirmer(nil, notFound())
	missing := c1.ConfirmDurable(context.Background(), testID).Error()

	c2, _ := confirmer(nil, errors.New("connection refused"))
	unknown := c2.ConfirmDurable(context.Background(), testID).Error()

	if missing == unknown {
		t.Fatal("a record that is absent and a record that could not be read produce the same message")
	}
	if strings.Contains(missing, "durability is unknown") {
		t.Error("a NotFound is not an unknown: the server answered, and the answer was no")
	}
	if strings.Contains(unknown, "never landed") {
		t.Error("a failed read must not claim the write never landed; it does not know that")
	}
}

// A wrapped NotFound must still be read as NotFound. journal.Store.Get returns the client error
// unwrapped today, but an adapter that decorates it later must not silently reclassify a missing
// record as an unknown read.
func TestAWrappedNotFoundIsStillNotFound(t *testing.T) {
	c, _ := confirmer(nil, &wrapped{notFound()})
	err := c.ConfirmDurable(context.Background(), testID)
	if err == nil || !strings.Contains(err.Error(), "never landed") {
		t.Fatalf("a wrapped NotFound must take the absent arm; got %v", err)
	}
}

type wrapped struct{ err error }

func (w *wrapped) Error() string { return "reading record: " + w.err.Error() }
func (w *wrapped) Unwrap() error { return w.err }

// --- the wiring itself --------------------------------------------------------------------------

// A misconfigured confirmer refuses rather than confirming, and it refuses WITHOUT reading. The
// direction matters: this is the same shape as the nil Accountant in P9-T7c-3d-ii-a. A zero-valued
// dependency that answered "fine" would switch the write-ahead rule off for anyone who forgot to
// wire it, and forgetting is exactly what happened for the whole of Phase 9 until now.
func TestAMisconfiguredConfirmerRefuses(t *testing.T) {
	cases := []struct {
		name string
		c    *Confirmer
		want string
	}{
		{"nil receiver", nil, "no journal reader is configured"},
		{"no reader", &Confirmer{Namespace: testNS}, "no journal reader is configured"},
		{"no namespace", &Confirmer{Records: &fakeReader{ar: durable()}}, "nowhere to look"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.c.ConfirmDurable(context.Background(), testID)
			if err == nil {
				t.Fatal("want a refusal, got nil")
			}
			if !errors.Is(err, ErrNotDurable) || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want a not-durable refusal containing %q", err, tc.want)
			}
		})
	}
}

func TestAMisconfiguredConfirmerNeverReads(t *testing.T) {
	r := &fakeReader{ar: durable()}
	c := &Confirmer{Records: r} // no namespace
	_ = c.ConfirmDurable(context.Background(), testID)
	if r.call != 0 {
		t.Fatalf("a confirmer with no namespace must refuse before reading; it read %d times", r.call)
	}
}

// The record is looked up in the broker's OWN namespace, never one the caller supplied. There is no
// namespace parameter on the interface, which is what makes this true by construction -- this test
// exists so that a future signature change cannot quietly add one.
func TestTheNamespaceIsTheConfirmersOwn(t *testing.T) {
	r := &fakeReader{ar: durable()}
	c := &Confirmer{Records: r, Namespace: "platform-system"}
	_ = c.ConfirmDurable(context.Background(), testID)
	if r.ns != "platform-system" {
		t.Fatalf("read namespace %q, want the confirmer's own %q", r.ns, "platform-system")
	}
}

func TestConfirmerIsAnExecuteJournal(t *testing.T) {
	// Compile-time in writeahead.go; restated here so the reason survives a refactor that drops the
	// var. If execute.Journal gains a method, this file is where the breakage should be explained.
	var c any = &Confirmer{}
	if _, ok := c.(interface {
		ConfirmDurable(context.Context, string) error
	}); !ok {
		t.Fatal("Confirmer no longer satisfies execute.Journal's method set")
	}
}
