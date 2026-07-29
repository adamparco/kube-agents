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

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
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
// action id in spec, and the Executing status label journal.Labels writes at Create time.
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
	for _, o := range opts {
		o(ar)
	}
	return ar
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

// The phase label is absent on any record whose caller did not set a phase before Create, and that
// is not an error. Only a NAMED and different phase is.
func TestAnUnlabelledRecordConfirms(t *testing.T) {
	ar := durable(func(a *agentv1alpha1.ActionRecord) { delete(a.Labels, journal.StatusLabel) })
	c, _ := confirmer(ar, nil)
	if err := c.ConfirmDurable(context.Background(), testID); err != nil {
		t.Fatalf("an absent status label is not a phase mismatch; got %v", err)
	}
}

func TestANilLabelMapConfirms(t *testing.T) {
	ar := durable(func(a *agentv1alpha1.ActionRecord) { a.Labels = nil })
	c, _ := confirmer(ar, nil)
	if err := c.ConfirmDurable(context.Background(), testID); err != nil {
		t.Fatalf("a record with no labels at all must not panic or refuse; got %v", err)
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
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Labels[journal.StatusLabel] = string(agentv1alpha1.PhasePendingApproval)
			}),
			want: `is in phase "PendingApproval"`,
		},
		{
			name: "the durable record was already rejected",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Labels[journal.StatusLabel] = string(agentv1alpha1.PhaseRejected)
			}),
			want: `is in phase "Rejected"`,
		},
		{
			name: "the durable record is already finished",
			ar: durable(func(a *agentv1alpha1.ActionRecord) {
				a.Labels[journal.StatusLabel] = string(agentv1alpha1.PhaseVerified)
			}),
			want: `is in phase "Verified"`,
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
