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

package verify

import (
	"errors"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
)

// TestTheNineNamedCausesAreAllPresent asserts the 04 §5.1 table was transcribed whole. Written as
// an explicit list rather than a range over NamedCauses(), so a cause silently dropped from the map
// fails here instead of shrinking the loop.
func TestTheNineNamedCausesAreAllPresent(t *testing.T) {
	want := map[Cause]Disposition{
		CauseConflict:             Transient,
		CauseThrottled:            Transient,
		CauseDependencyConverging: Transient,
		CauseCapacityArriving:     Transient,

		CauseSchemaRejected:      Terminal,
		CauseAdmissionDenied:     Terminal,
		CauseQuotaExhausted:      Terminal,
		CauseImageMissing:        Terminal,
		CauseSettleWindowExpired: Terminal,
	}
	if got := len(NamedCauses()); got != len(want) {
		t.Fatalf("NamedCauses() has %d entries, want the %d of 04 §5.1", got, len(want))
	}
	for c, d := range want {
		if got := DispositionOf(c); got != d {
			t.Errorf("DispositionOf(%s) = %s, want %s", c, got, d)
		}
	}
	for _, c := range NamedCauses() {
		if _, ok := want[c]; !ok {
			t.Errorf("%s is in the table but not in 04 §5.1's nine", c)
		}
	}
}

func TestUnknownCausesWait(t *testing.T) {
	// The direction matters: Terminal means "take a second unreviewed mutating action right now".
	if got := DispositionOf(CauseUnknown); got != Transient {
		t.Errorf("DispositionOf(Unknown) = %s, want Transient", got)
	}
	if got := DispositionOf(Cause("SomethingNobodyHasSeen")); got != Transient {
		t.Errorf("an unrecognized cause is %s, want Transient", got)
	}
	// ...and the wait is bounded, which is the only reason the above is safe.
	if got := DispositionOf(CauseSettleWindowExpired); got != Terminal {
		t.Fatalf("SettleWindowExpired is %s: with it transient, an unknown cause waits forever", got)
	}
}

func TestCauseOfReadsStructuredSignalsBeforeText(t *testing.T) {
	gr := schema.GroupResource{Group: "apps", Resource: "deployments"}
	invalid := apierrors.NewInvalid(
		schema.GroupKind{Group: "apps", Kind: "Deployment"}, "web",
		field.ErrorList{field.Required(field.NewPath("spec", "selector"), "")})

	cases := []struct {
		name string
		in   Failure
		want Cause
	}{
		{"conflict", Failure{Err: apierrors.NewConflict(gr, "web", errors.New("modified"))}, CauseConflict},
		{"throttled", Failure{Err: apierrors.NewTooManyRequests("slow down", 1)}, CauseThrottled},
		{"server timeout", Failure{Err: apierrors.NewServerTimeout(gr, "get", 1)}, CauseThrottled},
		{"invalid", Failure{Err: invalid}, CauseSchemaRejected},
		{"bad request", Failure{Err: apierrors.NewBadRequest("malformed")}, CauseSchemaRejected},
		{
			"admission webhook denial",
			Failure{Err: apierrors.NewForbidden(gr, "web",
				errors.New(`admission webhook "policy.example.com" denied the request`))},
			CauseAdmissionDenied,
		},
		{
			"quota with capacity exhausted",
			Failure{
				Err:      apierrors.NewForbidden(gr, "web", errors.New("exceeded quota: compute, requested: cpu=2")),
				Capacity: CapacityExhausted,
			},
			CauseQuotaExhausted,
		},
		{"image pull error", Failure{WaitingReason: "ErrImagePull"}, CauseImageMissing},
		{"image pull backoff", Failure{WaitingReason: "ImagePullBackOff"}, CauseImageMissing},
		{"invalid image name", Failure{WaitingReason: "InvalidImageName"}, CauseImageMissing},
		{"registry down is not a bad reference", Failure{WaitingReason: "RegistryUnavailable"}, CauseDependencyConverging},
		{"no signal at all", Failure{}, CauseUnknown},
		{"unparsed prose", Failure{Message: "something went sideways"}, CauseUnknown},
		{
			"scheduler message, capacity arriving",
			Failure{Message: "0/3 nodes are available: 3 Insufficient cpu.", Capacity: CapacityArriving},
			CauseCapacityArriving,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := CauseOf(tc.in); got != tc.want {
				t.Errorf("CauseOf = %s, want %s", got, tc.want)
			}
		})
	}
}

// TestCapacitySignalIsThreeValued is the T-10 guard. 09 §12 forbids a harness from picking its own
// number for an unresolved tightening, and the shape of "picking one" here would be folding Unknown
// into Exhausted -- which turns an open spec row into an automatic rollback nobody chose.
func TestCapacitySignalIsThreeValued(t *testing.T) {
	msg := "0/5 nodes are available: 5 Insufficient memory."
	for _, tc := range []struct {
		sig  CapacitySignal
		want Cause
		disp Disposition
	}{
		{CapacityArriving, CauseCapacityArriving, Transient},
		{CapacityExhausted, CauseQuotaExhausted, Terminal},
		{CapacityUnknown, CauseDependencyConverging, Transient},
		{CapacitySignal(""), CauseDependencyConverging, Transient},
	} {
		got := CauseOf(Failure{Message: msg, Capacity: tc.sig})
		if got != tc.want {
			t.Errorf("capacity=%q gives %s, want %s", tc.sig, got, tc.want)
		}
		if d := DispositionOf(got); d != tc.disp {
			t.Errorf("capacity=%q resolves to a %s cause, want %s", tc.sig, d, tc.disp)
		}
	}
}

// TestSettleWindowExpiryOutranksEverything is the backstop's own control: it must win even when the
// failure carries a signal that would otherwise read transient, or the wait never ends.
func TestSettleWindowExpiryOutranksEverything(t *testing.T) {
	f := Failure{
		Err:                 apierrors.NewConflict(schema.GroupResource{Resource: "pods"}, "p", errors.New("x")),
		Message:             "0/3 nodes are available",
		Capacity:            CapacityArriving,
		SettleWindowExpired: true,
	}
	if got := CauseOf(f); got != CauseSettleWindowExpired {
		t.Fatalf("CauseOf = %s over an expired window, want SettleWindowExpired", got)
	}
	if got := DispositionOf(CauseOf(f)); got != Terminal {
		t.Fatalf("an expired window is %s, want Terminal", got)
	}

	// The negative control: without the flag the same failure is transient. If it were not, the test
	// above would pass for a CauseOf that ignored every input and always answered "expired".
	f.SettleWindowExpired = false
	if got := DispositionOf(CauseOf(f)); got != Transient {
		t.Fatalf("the same failure inside the window is %s, want Transient", got)
	}
}

func TestAPlainRBACForbiddenIsNotAnAdmissionDenial(t *testing.T) {
	// Both are 403. Reading every 403 as a policy denial would make an under-provisioned broker SA
	// look like a rejected action, and roll back a change that never reached admission.
	err := apierrors.NewForbidden(
		schema.GroupResource{Group: "apps", Resource: "deployments"}, "web",
		errors.New(`User "sa" cannot patch resource "deployments"`))
	if got := CauseOf(Failure{Err: err}); got == CauseAdmissionDenied {
		t.Error("a plain RBAC forbidden was classified as an admission denial")
	}
}

func TestDescribeCarriesTheDisposition(t *testing.T) {
	// This string is what lands in the recovery transition's reason, and it is the only place an
	// operator reading the record learns why the ladder moved where it did.
	if got := Describe(CauseQuotaExhausted); got != "QuotaExhausted (Terminal)" {
		t.Errorf("Describe = %q", got)
	}
	if got := Describe(CauseConflict); got != "Conflict (Transient)" {
		t.Errorf("Describe = %q", got)
	}
}

func TestStatusReasonHelperMatches(t *testing.T) {
	err := apierrors.NewForbidden(schema.GroupResource{Resource: "pods"}, "p", errors.New("nope"))
	if !reasonIs(err, metav1.StatusReasonForbidden) {
		t.Error("reasonIs did not recognize a Forbidden status")
	}
}
