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

package webhook

import (
	"context"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The admission surface of the three brake objects (06 §4.4).
//
// These webhooks are `failurePolicy=Ignore`, which is unusual in this repository and is the reason
// the tests below look the way they do. Every rule a brake object may NOT violate lives in CRD-level
// CEL and holds with this process dead; what is left here is the class of mistake that produces a
// well-formed object which does not do what its author believed. A freeze whose scope is wider than
// it reads. A roster that can never reach its own approval threshold. An `expiresAt` copied from
// yesterday.
//
// So, as with the ChangePolicy webhook, the assertions are about the TEXT of the error or warning
// and not about a boolean. The person typing a FleetFreeze is doing it during an incident, from a
// phone, under time pressure. "admission webhook denied the request" tells them nothing they can act
// on; the message has to name the field and the consequence.

func freeze(scope agentv1alpha1.FreezeScope) *agentv1alpha1.FleetFreeze {
	return &agentv1alpha1.FleetFreeze{
		ObjectMeta: metav1.ObjectMeta{Name: "inc-4471"},
		Spec: agentv1alpha1.FleetFreezeSpec{
			Scope:       scope,
			Reason:      "INC-4471 — payments degraded, stopping all agent writes",
			RequestedBy: "slack:U0INCIDENT",
		},
	}
}

func roster(approvers []agentv1alpha1.Approver) *agentv1alpha1.ApprovalRoster {
	return &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "team-x-approvers", Namespace: "team-x"},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: approvers,
			Notify:    &agentv1alpha1.ApprovalNotify{Slack: &agentv1alpha1.SlackNotify{Channel: "#team-x-ops"}},
		},
	}
}

func approver(platform agentv1alpha1.ApproverPlatform, id string) agentv1alpha1.Approver {
	return agentv1alpha1.Approver{Platform: platform, ID: id}
}

func undo(requestedBy string) *agentv1alpha1.UndoRequest {
	return &agentv1alpha1.UndoRequest{
		ObjectMeta: metav1.ObjectMeta{Name: "undo-1", Namespace: "team-x"},
		Spec: agentv1alpha1.UndoRequestSpec{
			ActionRef:   agentv1alpha1.ActionRef{Name: "01JQ0000000000000000000000"},
			Reason:      "the scale-down was correct but landed during the incident",
			RequestedBy: requestedBy,
		},
	}
}

// wantRejected asserts the object is refused AND that the message names each fragment. A rejection
// with an unhelpful message is a half-failure this repository has decided to treat as a failure.
func wantRejected(t *testing.T, err error, what string, fragments ...string) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s was admitted; expected a rejection mentioning %v", what, fragments)
	}
	for _, f := range fragments {
		if !strings.Contains(err.Error(), f) {
			t.Errorf("%s: rejection does not mention %q.\ngot: %v", what, f, err)
		}
	}
}

func wantAdmitted(t *testing.T, err error, what string) {
	t.Helper()
	if err != nil {
		t.Fatalf("%s was rejected: %v", what, err)
	}
}

// wantWarning asserts exactly that one warning matching `fragment` is present. Counting matters:
// a warning emitted twice is a bug that reads as a formatting quirk, and warnings are the only
// output these webhooks have for the cases they deliberately do not refuse.
func wantWarning(t *testing.T, warnings []string, fragment string, what string) {
	t.Helper()
	n := 0
	for _, w := range warnings {
		if strings.Contains(w, fragment) {
			n++
		}
	}
	if n != 1 {
		t.Errorf("%s: want exactly 1 warning containing %q, got %d.\nwarnings: %v", what, fragment, n, warnings)
	}
}

func wantNoWarning(t *testing.T, warnings []string, fragment string, what string) {
	t.Helper()
	for _, w := range warnings {
		if strings.Contains(w, fragment) {
			t.Errorf("%s: unexpected warning containing %q: %q", what, fragment, w)
		}
	}
}

// ---------------------------------------------------------------------------------------------
// FleetFreeze
// ---------------------------------------------------------------------------------------------

// TestFleetFreezeScopeHolesAreRefused is the highest-value assertion in this file.
//
// 06 §4.4 says a FleetFreeze scope widens when a field is omitted, which makes `{}` the whole fleet
// and is correct. The consequence nobody expects is what a PARTIALLY filled scope means:
// `{clusterName: prod}` with no projectId matches a cluster named `prod` in every project the
// operator can see. The YAML reads as "freeze prod". The object freezes considerably more than prod.
//
// It is refused rather than warned because a warning during an incident is a line of text above the
// output of a command that already succeeded, and this is the one object whose blast radius has to
// be legible from the file.
func TestFleetFreezeScopeHolesAreRefused(t *testing.T) {
	t.Run("cluster without project", func(t *testing.T) {
		_, err := ValidateFleetFreeze(freeze(agentv1alpha1.FreezeScope{ClusterName: "prod"}))
		wantRejected(t, err, "a freeze naming a cluster but no project",
			"spec.scope.projectId", "EVERY project")
	})

	t.Run("namespace without cluster", func(t *testing.T) {
		_, err := ValidateFleetFreeze(freeze(agentv1alpha1.FreezeScope{
			ProjectID: "adamparco-kage", Namespace: "team-x",
		}))
		wantRejected(t, err, "a freeze naming a namespace but no cluster",
			"spec.scope.clusterName", "every namespace with that name")
	})

	t.Run("namespace with neither", func(t *testing.T) {
		_, err := ValidateFleetFreeze(freeze(agentv1alpha1.FreezeScope{Namespace: "team-x"}))
		wantRejected(t, err, "a freeze naming only a namespace", "spec.scope")
	})

	// The three scopes that are actually well-formed. Without these the rules above are satisfied by
	// a validator that refuses every FleetFreeze, and the brake would be unusable in the direction
	// nobody tests during an incident.
	for name, scope := range map[string]agentv1alpha1.FreezeScope{
		"the whole fleet":  {},
		"a whole project":  {ProjectID: "adamparco-kage"},
		"a single cluster": {ProjectID: "adamparco-kage", ClusterName: "prod"},
		"a namespace":      {ProjectID: "adamparco-kage", ClusterName: "prod", Namespace: "team-x"},
	} {
		t.Run("admitted: "+name, func(t *testing.T) {
			_, err := ValidateFleetFreeze(freeze(scope))
			wantAdmitted(t, err, "freeze scoped to "+name)
		})
	}
}

// TestFleetFreezeWarnsOnTheWholeFleet: `{}` is both the documented way to freeze everything and what
// an unfilled template looks like. It cannot be refused -- fleet-wide is the flagship use -- so the
// only available signal is a warning, and the warning must not fire on the narrower scopes or it
// becomes noise that gets filtered.
func TestFleetFreezeWarnsOnTheWholeFleet(t *testing.T) {
	w, err := ValidateFleetFreeze(freeze(agentv1alpha1.FreezeScope{}))
	wantAdmitted(t, err, "a fleet-wide freeze")
	wantWarning(t, w, "THE ENTIRE FLEET", "empty scope")

	w, err = ValidateFleetFreeze(freeze(agentv1alpha1.FreezeScope{ProjectID: "adamparco-kage"}))
	wantAdmitted(t, err, "a project-wide freeze")
	wantNoWarning(t, w, "THE ENTIRE FLEET", "project-scoped freeze")
}

// TestFleetFreezeWarnsWhenCreatedAlreadyExpired drives the `brakeNow` seam.
//
// The seam exists so this test can exist. A warning about a past timestamp is only reachable by
// waiting unless the clock is injectable, and a warning nobody has watched fire is a warning nobody
// knows works -- which for this one matters more than usual: an expired freeze is admitted, reports
// healthy, and freezes nothing. It is the single likeliest FleetFreeze mistake (a copied timestamp,
// or a timezone read the wrong way) and the object gives no other sign.
func TestFleetFreezeWarnsWhenCreatedAlreadyExpired(t *testing.T) {
	fixed := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	orig := brakeNow
	brakeNow = func() time.Time { return fixed }
	t.Cleanup(func() { brakeNow = orig })

	cases := []struct {
		name        string
		expiresAt   *metav1.Time
		wantWarning bool
	}{
		{"an hour ago", ptr.To(metav1.NewTime(fixed.Add(-time.Hour))), true},
		{"exactly now", ptr.To(metav1.NewTime(fixed)), true}, // not After(now): already over
		{"in an hour", ptr.To(metav1.NewTime(fixed.Add(time.Hour))), false},
		{"never expires", nil, false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ff := freeze(agentv1alpha1.FreezeScope{ProjectID: "adamparco-kage"})
			ff.Spec.ExpiresAt = tc.expiresAt

			w, err := ValidateFleetFreeze(ff)
			wantAdmitted(t, err, "a freeze expiring "+tc.name)
			if tc.wantWarning {
				wantWarning(t, w, "already in the past", "expiresAt "+tc.name)
			} else {
				wantNoWarning(t, w, "already in the past", "expiresAt "+tc.name)
			}
		})
	}
}

// TestFleetFreezeDeleteIsAlwaysPermitted. Deleting the object IS how a freeze is lifted -- there is
// no `enabled: false` -- so a validator that could refuse the delete could strand the fleet, during
// the incident that produced the freeze, with etcd as the only remaining way out.
func TestFleetFreezeDeleteIsAlwaysPermitted(t *testing.T) {
	v := &FleetFreezeCustomValidator{}
	if _, err := v.ValidateDelete(context.Background(), freeze(agentv1alpha1.FreezeScope{})); err != nil {
		t.Fatalf("deleting a FleetFreeze was refused: %v — lifting a freeze must never require etcd surgery", err)
	}
}

// ---------------------------------------------------------------------------------------------
// ApprovalRoster
// ---------------------------------------------------------------------------------------------

// TestRosterThatCanNeverApprove is the gate-becomes-a-wall case, and it is the reason this webhook
// is worth having at all.
//
// `minApprovals: 3` on a two-person roster is not a strict policy. No set of approvals can satisfy
// it, so every gated action parks as PendingApproval and expires. Expiry is never an approval
// (06 §4.4), so nothing executes and nothing raises an alarm: the agent simply loses the ability to
// do anything gated, and both the roster and the agent look healthy the whole time.
//
// CEL cannot express it -- it is a cross-field rule between an integer and the length of a list --
// so if it is not here it is nowhere.
func TestRosterThatCanNeverApprove(t *testing.T) {
	r := roster([]agentv1alpha1.Approver{
		approver(agentv1alpha1.ApproverPlatformSlack, "U01"),
		approver(agentv1alpha1.ApproverPlatformSlack, "U02"),
	})
	r.Spec.MinApprovals = ptr.To(int32(3))

	_, err := ValidateApprovalRoster(r)
	wantRejected(t, err, "a roster needing 3 approvals from 2 approvers",
		"spec.minApprovals", "expire unreviewed")

	// Exactly at the boundary is legitimate: unanimity is a real policy.
	r.Spec.MinApprovals = ptr.To(int32(2))
	_, err = ValidateApprovalRoster(r)
	wantAdmitted(t, err, "a roster needing 2 approvals from 2 approvers")
}

// TestRosterDuplicatesAreRefused. `minApprovals` counts DISTINCT principals, so a roster listing one
// person twice with `minApprovals: 2` looks like four-eyes and is one pair of eyes. Refused rather
// than silently deduplicated, because deduplication hands the author a roster that passes a policy
// review it does not meet.
func TestRosterDuplicatesAreRefused(t *testing.T) {
	_, err := ValidateApprovalRoster(roster([]agentv1alpha1.Approver{
		approver(agentv1alpha1.ApproverPlatformSlack, "U01"),
		approver(agentv1alpha1.ApproverPlatformSlack, "U02"),
		approver(agentv1alpha1.ApproverPlatformSlack, "U01"),
	}))
	wantRejected(t, err, "a roster listing slack:U01 twice",
		"spec.approvers[2].id", "DISTINCT principals")

	// The same ID on two DIFFERENT platforms is two principals, because `Principal()` qualifies it.
	// Someone with the same handle in Slack and Google Chat is one human, but the system cannot know
	// that, and guessing in the permissive direction would be the wrong guess.
	_, err = ValidateApprovalRoster(roster([]agentv1alpha1.Approver{
		approver(agentv1alpha1.ApproverPlatformSlack, "alice"),
		approver(agentv1alpha1.ApproverPlatformGoogleChat, "alice"),
	}))
	wantAdmitted(t, err, "the same id on two platforms")
}

// TestRosterTTLBoundsAreRefusedNotClamped pins the asymmetry between admission and runtime.
//
// `EffectiveTTL()` CLAMPS an out-of-range TTL; admission REFUSES it. Both are correct because they
// answer different questions. Admission is asked "is this what you meant?", and an author who wrote
// `ttl: 5m` did not mean an hour. The runtime is asked "what do I do with the object that is already
// stored", where refusing to evaluate would leave the gated action parked forever with no expiry --
// failing open on the one property that matters.
func TestRosterTTLBoundsAreRefusedNotClamped(t *testing.T) {
	cases := []struct {
		name     string
		ttl      time.Duration
		fragment string
	}{
		{"below the floor", 5 * time.Minute, "floor"},
		{"just below the floor", agentv1alpha1.MinApprovalTTL - time.Second, "floor"},
		{"above the ceiling", 7 * 24 * time.Hour, "ceiling"},
		{"just above the ceiling", agentv1alpha1.MaxApprovalTTL + time.Second, "ceiling"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := roster([]agentv1alpha1.Approver{approver(agentv1alpha1.ApproverPlatformSlack, "U01")})
			r.Spec.TTL = &metav1.Duration{Duration: tc.ttl}

			_, err := ValidateApprovalRoster(r)
			wantRejected(t, err, "a roster with ttl "+tc.ttl.String(), "spec.ttl", tc.fragment)

			// The runtime, given the very same object, clamps into range rather than refusing.
			if got := r.EffectiveTTL(); got < agentv1alpha1.MinApprovalTTL || got > agentv1alpha1.MaxApprovalTTL {
				t.Errorf("EffectiveTTL() = %s for a stored roster with ttl %s: the runtime must clamp "+
					"into [%s, %s], because a broker that cannot compute an expiry leaves the action "+
					"parked forever", got, tc.ttl, agentv1alpha1.MinApprovalTTL, agentv1alpha1.MaxApprovalTTL)
			}
		})
	}

	// Both boundaries, and the default, are admitted. Without this the rule above is satisfied by a
	// validator that refuses every TTL.
	for _, ok := range []time.Duration{
		agentv1alpha1.MinApprovalTTL,
		agentv1alpha1.DefaultApprovalTTL,
		agentv1alpha1.MaxApprovalTTL,
	} {
		r := roster([]agentv1alpha1.Approver{approver(agentv1alpha1.ApproverPlatformSlack, "U01")})
		r.Spec.TTL = &metav1.Duration{Duration: ok}
		if _, err := ValidateApprovalRoster(r); err != nil {
			t.Errorf("in-range ttl %s was refused: %v", ok, err)
		}
	}
}

// TestRosterWarnsOnSelfApprovalAlone and on an undeliverable roster. Neither is refused: a one-person
// team is real, and notification is delivery rather than authorisation. Both are warned about,
// because in the YAML they are indistinguishable from a roster that enforces review and one that
// reaches somebody.
func TestRosterWarnsWithoutRefusing(t *testing.T) {
	t.Run("self-approval on a single-approver roster", func(t *testing.T) {
		r := roster([]agentv1alpha1.Approver{approver(agentv1alpha1.ApproverPlatformSlack, "U01")})
		r.Spec.AllowSelfApproval = ptr.To(true)

		w, err := ValidateApprovalRoster(r)
		wantAdmitted(t, err, "a self-approving single-approver roster")
		wantWarning(t, w, "it is not review", "self-approval alone")

		// Two approvers with self-approval is a real four-eyes policy that happens to let the
		// requester be one of the two. Warning there would train people to ignore the warning.
		r.Spec.Approvers = append(r.Spec.Approvers, approver(agentv1alpha1.ApproverPlatformSlack, "U02"))
		w, err = ValidateApprovalRoster(r)
		wantAdmitted(t, err, "a self-approving two-approver roster")
		wantNoWarning(t, w, "it is not review", "self-approval with two approvers")
	})

	t.Run("no notify destination", func(t *testing.T) {
		r := roster([]agentv1alpha1.Approver{approver(agentv1alpha1.ApproverPlatformSlack, "U01")})
		r.Spec.Notify = nil

		w, err := ValidateApprovalRoster(r)
		wantAdmitted(t, err, "a roster with no notify block")
		wantWarning(t, w, "names no destination", "nil notify")

		// An empty notify block is the same mistake wearing more YAML.
		r.Spec.Notify = &agentv1alpha1.ApprovalNotify{}
		w, _ = ValidateApprovalRoster(r)
		wantWarning(t, w, "names no destination", "empty notify")

		// And a roster that CAN be reached must be quiet.
		r.Spec.Notify = &agentv1alpha1.ApprovalNotify{GoogleChat: &agentv1alpha1.GoogleChatNotify{Space: "spaces/AAA"}}
		w, _ = ValidateApprovalRoster(r)
		wantNoWarning(t, w, "names no destination", "google chat notify")
	})
}

// ---------------------------------------------------------------------------------------------
// UndoRequest
// ---------------------------------------------------------------------------------------------

// TestUndoRequestIsAdmittedEvenWhenItCannotSucceed pins the thinness of this validator as a
// DELIBERATE property rather than an oversight, because the obvious "improvement" is to look the
// ActionRecord up here and refuse if it is missing.
//
// That improvement would break the brake. 06 §4.4 requires undo to work through `kubectl` with
// everything else down; an admission-time cross-object read makes undo admission depend on an API
// read at exactly the moment the cluster is unhealthy. And the refusal it buys is worse than the one
// it replaces: an admission error appears in a terminal somebody may not be watching, where
// `phase: Refused` with a message is a durable object that explains itself.
func TestUndoRequestIsAdmittedEvenWhenItCannotSucceed(t *testing.T) {
	ur := undo("slack:U0INCIDENT")
	ur.Spec.ActionRef.Name = "01ZZZZZZZZZZZZZZZZZZZZZZZZ" // no such action exists anywhere

	if _, err := ValidateUndoRequest(ur); err != nil {
		t.Fatalf("an UndoRequest naming a nonexistent action was refused at admission: %v\n"+
			"It must be admitted and refused by the controller with phase: Refused — undo has to work "+
			"through kubectl with the rest of the system down (06 §4.4)", err)
	}
}

// TestUndoRequestWarnings. Two, both about authorisation and both about consequences the author
// cannot see in their own YAML.
func TestUndoRequestWarnings(t *testing.T) {
	t.Run("k8s: principal falls back to RBAC", func(t *testing.T) {
		w, err := ValidateUndoRequest(undo("k8s:alice@example.com"))
		wantAdmitted(t, err, "an undo requested by a k8s: principal")
		wantWarning(t, w, "Kubernetes RBAC", "k8s: principal")

		w, err = ValidateUndoRequest(undo("slack:U01"))
		wantAdmitted(t, err, "an undo requested by a slack: principal")
		wantNoWarning(t, w, "Kubernetes RBAC", "slack: principal")
	})

	t.Run("markContested false invites a redo loop", func(t *testing.T) {
		ur := undo("slack:U01")
		ur.Spec.MarkContested = ptr.To(false)

		w, err := ValidateUndoRequest(ur)
		wantAdmitted(t, err, "an undo with markContested false")
		wantWarning(t, w, "may legitimately redo", "markContested false")

		// Unset means true, so it must be quiet -- this is the common path and the safe one.
		w, _ = ValidateUndoRequest(undo("slack:U01"))
		wantNoWarning(t, w, "may legitimately redo", "markContested unset")
	})
}

// TestBrakeValidatorsRejectTheWrongType is gone: admission.Validator is now generic
// (admission.Validator[*FleetFreeze] etc.), so passing an *Agent to ValidateCreate/ValidateUpdate
// is a compile error, not a runtime type assertion this test could exercise. The property the test
// checked -- a misrouted object cannot sail through a failurePolicy=Ignore webhook as a nil error --
// is now enforced by the compiler instead.
