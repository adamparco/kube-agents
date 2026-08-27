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

package controller_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The three brake objects' schema-level validations, against a real API server (06 §4.4).
//
// This file exists for a sharper reason than the ChangePolicy equivalent. The ChangePolicy webhook
// is `failurePolicy: fail`, so its CRD rules are a second line of defence for an unusual day. The
// brake webhooks are `failurePolicy: Ignore` BY DESIGN -- a `Fail` policy would mean that killing
// the operator makes it impossible to CREATE a FleetFreeze, turning a control-plane outage into a
// brake outage. The consequence is that for these three objects the CRD is not the backstop. It is
// the ONLY enforcement that is guaranteed to be running at the moment somebody needs the brake, and
// the operator being down is the most likely reason they need it.
//
// So everything asserted below is asserted through a real API server with no webhook installed,
// because that is the configuration these rules have to hold in. And everything is a pair: a test
// that only asserted rejections would pass against a CRD that rejects every FleetFreeze, which is
// the same outage as having no brake at all, discovered at the same moment.

func brakeEnv(t *testing.T) (client.Client, context.Context) {
	t.Helper()
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the brake schema rules")
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}

	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest (a CEL compile error in one of the brake CRDs surfaces here, as a CRD-install failure): %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	return k8s, context.Background()
}

// brakeNamespace creates the tenant namespace the two namespaced brake objects live in. A real
// namespace rather than `default`, because "namespaced" is one of the properties under test and
// `default` exists whether or not the CRD says so.
func brakeNamespace(t *testing.T, ctx context.Context, k8s client.Client, name string) {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
	if err := k8s.Create(ctx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create namespace %s: %v", name, err)
	}
}

// ---------------------------------------------------------------------------------------------
// FleetFreeze
// ---------------------------------------------------------------------------------------------

func TestFleetFreezeCEL(t *testing.T) {
	k8s, ctx := brakeEnv(t)

	create := func(t *testing.T, ff *agentv1alpha1.FleetFreeze) error {
		t.Helper()
		err := k8s.Create(ctx, ff)
		if err == nil {
			t.Cleanup(func() { _ = k8s.Delete(ctx, ff) })
		}
		return err
	}
	newFreeze := func(name string) *agentv1alpha1.FleetFreeze {
		return &agentv1alpha1.FleetFreeze{
			ObjectMeta: metav1.ObjectMeta{Name: name},
			Spec: agentv1alpha1.FleetFreezeSpec{
				Reason:      "INC-4471 — payments degraded",
				RequestedBy: "slack:U0INCIDENT",
			},
		}
	}

	t.Run("the incident-time minimum is accepted", func(t *testing.T) {
		// The whole object somebody types from a phone: a reason, who asked, and nothing else. If this
		// is ever rejected the brake does not exist, whatever else in this file passes.
		if err := create(t, newFreeze("inc-minimal")); err != nil {
			t.Fatalf("the minimal fleet-wide freeze was rejected: %v", err)
		}
	})

	t.Run("cluster-scoped", func(t *testing.T) {
		// Namespaced would be wrong twice: a fleet-wide freeze has no natural namespace, and it would
		// make the object editable by anyone with write on whichever namespace it landed in.
		if err := create(t, newFreeze("scope-check")); err != nil {
			t.Fatalf("create: %v", err)
		}
		var got agentv1alpha1.FleetFreeze
		if err := k8s.Get(ctx, client.ObjectKey{Name: "scope-check"}, &got); err != nil {
			t.Fatalf("get by name alone failed, so FleetFreeze is not cluster-scoped: %v", err)
		}
	})

	t.Run("allowClasses may name routine and nothing else", func(t *testing.T) {
		// The single-member enum is the specification, not an oversight (06 §4.4: allowClasses "may
		// list ONLY routine; never gated"). A freeze that still lets gated actions through is not a
		// freeze -- and `gated` is the value somebody reaches for when they want "the risky stuff
		// still needs a human", which sounds careful and is the exact inversion of what a freeze is.
		ff := newFreeze("try-gated")
		ff.Spec.AllowClasses = []agentv1alpha1.FreezeClass{agentv1alpha1.FreezeClass("gated")}
		if err := create(t, ff); err == nil {
			t.Fatal("allowClasses: [gated] was stored. A freeze that admits gated actions is not a freeze (06 §4.4)")
		} else if !strings.Contains(err.Error(), "Unsupported value") {
			t.Fatalf("rejected, but not by the enum: %v", err)
		}

		ok := newFreeze("allow-routine")
		ok.Spec.AllowClasses = []agentv1alpha1.FreezeClass{agentv1alpha1.FreezeClassRoutine}
		if err := create(t, ok); err != nil {
			t.Fatalf("allowClasses: [routine] was rejected, so a read-only-but-still-working freeze cannot be expressed: %v", err)
		}
	})

	t.Run("an absent allowClasses means nothing executes", func(t *testing.T) {
		// The fail-closed default, and the one place in this API where "empty" narrows rather than
		// widens -- the reverse of what `scope` does one field up. Asserted by reading it back: the
		// API server must NOT have defaulted it to something permissive.
		if err := create(t, newFreeze("no-allow")); err != nil {
			t.Fatalf("create: %v", err)
		}
		var got agentv1alpha1.FleetFreeze
		if err := k8s.Get(ctx, client.ObjectKey{Name: "no-allow"}, &got); err != nil {
			t.Fatalf("get: %v", err)
		}
		if len(got.Spec.AllowClasses) != 0 {
			t.Fatalf("allowClasses defaulted to %v; an unspecified allowClasses must permit NOTHING, "+
				"because a default that lets routine actions through would make every freeze partial "+
				"without saying so", got.Spec.AllowClasses)
		}
		if !got.UndoAllowed() {
			t.Error("allowUndo defaulted to false: a freeze that also blocks undo traps whatever the " +
				"agent did immediately before it, which is usually why the freeze was applied")
		}
	})

	t.Run("requestedBy must be a platform-qualified principal", func(t *testing.T) {
		// 06 §1.2 V-11's form. An unqualified `alice` cannot be matched against any allowedUsers list
		// and cannot be attributed in the journal, and the freeze is the object where "who did this"
		// is asked most urgently.
		for _, bad := range []string{"alice", "email:alice@example.com", "slack:", ""} {
			ff := newFreeze("bad-principal")
			ff.Spec.RequestedBy = bad
			if err := create(t, ff); err == nil {
				t.Errorf("requestedBy %q was stored; it names nobody the journal can attribute this freeze to", bad)
				_ = k8s.Delete(ctx, ff)
			}
		}
		// All three platforms parse, including `k8s:` -- a human running kubectl with chat down has a
		// Kubernetes username and no Slack ID, and that is the API brake's whole scenario.
		for i, good := range []string{"slack:U01", "googlechat:users/123", "k8s:alice@example.com"} {
			ff := newFreeze("good-principal-" + string(rune('a'+i)))
			ff.Spec.RequestedBy = good
			if err := create(t, ff); err != nil {
				t.Errorf("requestedBy %q was rejected: %v", good, err)
			}
		}
	})

	t.Run("a freeze must say why", func(t *testing.T) {
		// Not bureaucracy: the reason is what the broker returns to every agent it refuses, and it is
		// what tells the next person on shift whether the freeze is still needed. An unexplained
		// freeze outlives its incident.
		ff := newFreeze("no-reason")
		ff.Spec.Reason = ""
		if err := create(t, ff); err == nil {
			t.Fatal("a FleetFreeze with an empty reason was stored; the reason is the refusal message every blocked agent receives")
		}
	})
}

// ---------------------------------------------------------------------------------------------
// ApprovalRoster
// ---------------------------------------------------------------------------------------------

func TestApprovalRosterCEL(t *testing.T) {
	k8s, ctx := brakeEnv(t)

	brakeNamespace(t, ctx, k8s, "team-x")

	create := func(t *testing.T, ar *agentv1alpha1.ApprovalRoster) error {
		t.Helper()
		err := k8s.Create(ctx, ar)
		if err == nil {
			t.Cleanup(func() { _ = k8s.Delete(ctx, ar) })
		}
		return err
	}
	newRoster := func(name string, approvers ...agentv1alpha1.Approver) *agentv1alpha1.ApprovalRoster {
		return &agentv1alpha1.ApprovalRoster{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "team-x"},
			Spec:       agentv1alpha1.ApprovalRosterSpec{Approvers: approvers},
		}
	}
	alice := agentv1alpha1.Approver{Platform: agentv1alpha1.ApproverPlatformSlack, ID: "U0ALICE"}

	t.Run("the defaults are the ones 06 §4.4 names", func(t *testing.T) {
		// Read back rather than asserted in Go, because these three defaults are declared by
		// kubebuilder markers and applied by the API server. A marker that fails to survive
		// `controller-gen` is invisible in the Go source and changes the meaning of every roster.
		if err := create(t, newRoster("defaults", alice)); err != nil {
			t.Fatalf("create: %v", err)
		}
		var got agentv1alpha1.ApprovalRoster
		if err := k8s.Get(ctx, client.ObjectKey{Name: "defaults", Namespace: "team-x"}, &got); err != nil {
			t.Fatalf("get: %v", err)
		}
		if got.EffectiveMinApprovals() != 1 {
			t.Errorf("minApprovals defaulted to %d, want 1", got.EffectiveMinApprovals())
		}
		if got.EffectiveTTL() != agentv1alpha1.DefaultApprovalTTL {
			t.Errorf("ttl defaulted to %s, want %s — 06 §4.4's single canonical default, which 04 §3.1 also references",
				got.EffectiveTTL(), agentv1alpha1.DefaultApprovalTTL)
		}
		if got.SelfApprovalAllowed() {
			t.Error("allowSelfApproval defaulted to TRUE. Four-eyes has to be what you get by not " +
				"thinking about it; a permissive default means every roster written in a hurry is a " +
				"roster with no second pair of eyes (06 §4.4 fail-closed rule 6)")
		}
	})

	t.Run("a roster with no approvers is refused", func(t *testing.T) {
		// The empty roster is exactly the fail-closed hazard of 06 §4.4 rule 6 written as YAML: it
		// LOOKS like an approval control and approves nothing. Refusing it at the CRD means the
		// mistake is impossible to store, rather than merely handled at runtime.
		if err := create(t, newRoster("empty")); err == nil {
			t.Fatal("an ApprovalRoster with no approvers was stored; it appears in the roster list and can never approve anything")
		}
	})

	t.Run("an approver id may not contain the principal separator", func(t *testing.T) {
		// `Principal()` joins platform and id with a colon, so an id containing one makes the
		// canonical form ambiguous: `slack:a:b` could parse two ways, and two parses of an approver
		// identity is one identity and one impersonation.
		for _, bad := range []string{"a:b", "has space", ""} {
			ar := newRoster("bad-id", agentv1alpha1.Approver{Platform: agentv1alpha1.ApproverPlatformSlack, ID: bad})
			if err := create(t, ar); err == nil {
				t.Errorf("approver id %q was stored; `<platform>:<id>` must parse back unambiguously", bad)
				_ = k8s.Delete(ctx, ar)
			}
		}
		// Google Chat ids are path-shaped, so slashes must be legal.
		if err := create(t, newRoster("gchat", agentv1alpha1.Approver{
			Platform: agentv1alpha1.ApproverPlatformGoogleChat, ID: "users/1234567890",
		})); err != nil {
			t.Fatalf("a path-shaped Google Chat id was rejected: %v", err)
		}
	})

	t.Run("an unknown platform is refused", func(t *testing.T) {
		ar := newRoster("email-approver", agentv1alpha1.Approver{
			Platform: agentv1alpha1.ApproverPlatform("email"), ID: "alice@example.com",
		})
		if err := create(t, ar); err == nil {
			t.Fatal("platform: email was stored; the broker cannot deliver to or verify a platform it does not implement, so the roster would silently never approve")
		}
	})

	t.Run("minApprovals is bounded", func(t *testing.T) {
		// Zero is the interesting bound: it reads as "no approvals needed", which is not a lenient
		// gate but a gate that has been removed while still appearing in the policy list.
		for _, bad := range []int32{0, -1, 129} {
			ar := newRoster("bad-min", alice)
			ar.Spec.MinApprovals = ptr.To(bad)
			if err := create(t, ar); err == nil {
				t.Errorf("minApprovals: %d was stored", bad)
				_ = k8s.Delete(ctx, ar)
			}
		}
	})

	t.Run("namespaced", func(t *testing.T) {
		// Unlike FleetFreeze and ChangePolicy. A roster names the humans who review a particular
		// team's changes, which is a per-namespace fact, and a cluster-scoped roster would need a
		// selector to say the same thing less clearly.
		if err := create(t, newRoster("ns-check", alice)); err != nil {
			t.Fatalf("create: %v", err)
		}
		var got agentv1alpha1.ApprovalRoster
		if err := k8s.Get(ctx, client.ObjectKey{Name: "ns-check"}, &got); err == nil {
			t.Fatal("get by name alone succeeded, so ApprovalRoster is cluster-scoped")
		}
	})
}

// ---------------------------------------------------------------------------------------------
// UndoRequest
// ---------------------------------------------------------------------------------------------

func TestUndoRequestCEL(t *testing.T) {
	k8s, ctx := brakeEnv(t)

	brakeNamespace(t, ctx, k8s, "team-x")

	newUndo := func(name string) *agentv1alpha1.UndoRequest {
		return &agentv1alpha1.UndoRequest{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "team-x"},
			Spec: agentv1alpha1.UndoRequestSpec{
				ActionRef:   agentv1alpha1.ActionRef{Name: "01JQ0000000000000000000000"},
				Reason:      "correct change, wrong moment",
				RequestedBy: "k8s:alice@example.com",
			},
		}
	}
	create := func(t *testing.T, ur *agentv1alpha1.UndoRequest) error {
		t.Helper()
		err := k8s.Create(ctx, ur)
		if err == nil {
			t.Cleanup(func() { _ = k8s.Delete(ctx, ur) })
		}
		return err
	}

	t.Run("kubectl-with-everything-down is accepted", func(t *testing.T) {
		// The scenario the object exists for: chat is down, the operator may be down, and a human
		// with a kubeconfig needs to reverse something. `k8s:` is the identity they have.
		if err := create(t, newUndo("kubectl-undo")); err != nil {
			t.Fatalf("the API-brake undo was rejected: %v", err)
		}
		var got agentv1alpha1.UndoRequest
		if err := k8s.Get(ctx, client.ObjectKey{Name: "kubectl-undo", Namespace: "team-x"}, &got); err != nil {
			t.Fatalf("get: %v", err)
		}
		if !got.ContestedRequested() {
			t.Error("markContested defaulted to false. Without the marker the human undoes, the agent " +
				"redoes on its next reconcile, and the human concludes the brake does not work")
		}
	})

	t.Run("the spec is immutable", func(t *testing.T) {
		// The status describes what was done about a SPECIFIC action. Repointing the spec afterwards
		// leaves an object whose status is a truthful record of something its spec no longer names --
		// and the journal, which reads this object, would attribute one undo to the wrong action.
		ur := newUndo("immutable")
		if err := create(t, ur); err != nil {
			t.Fatalf("create: %v", err)
		}

		var got agentv1alpha1.UndoRequest
		if err := k8s.Get(ctx, client.ObjectKey{Name: "immutable", Namespace: "team-x"}, &got); err != nil {
			t.Fatalf("get: %v", err)
		}
		got.Spec.Reason = "actually a different reason"
		if err := k8s.Update(ctx, &got); err == nil {
			t.Fatal("the spec of a stored UndoRequest was edited; create a new request rather than repointing one whose status already describes a different action")
		} else if !strings.Contains(err.Error(), "immutable") {
			t.Fatalf("rejected, but not by the immutability rule: %v", err)
		}

		// Labels and annotations must still be editable, or the object cannot be triaged, selected or
		// annotated by anything -- and a CEL rule over the whole object rather than the spec is an
		// easy and invisible way to get that wrong.
		if err := k8s.Get(ctx, client.ObjectKey{Name: "immutable", Namespace: "team-x"}, &got); err != nil {
			t.Fatalf("re-get: %v", err)
		}
		got.Labels = map[string]string{"incident": "inc-4471"}
		if err := k8s.Update(ctx, &got); err != nil {
			t.Fatalf("labelling an UndoRequest was refused, so spec immutability was applied to the whole object: %v", err)
		}
	})

	t.Run("actionRef and reason are required", func(t *testing.T) {
		noRef := newUndo("no-ref")
		noRef.Spec.ActionRef.Name = ""
		if err := create(t, noRef); err == nil {
			t.Error("an UndoRequest naming no action was stored")
		}
		noReason := newUndo("no-reason")
		noReason.Spec.Reason = ""
		if err := create(t, noReason); err == nil {
			t.Error("an UndoRequest with no reason was stored; the reason is journalled as the human's account of why the agent's change was reversed")
		}
	})

	t.Run("undoActionId must be a ULID", func(t *testing.T) {
		// Status, not spec, and written by the controller rather than a human -- which is exactly why
		// it is worth pinning. A controller that wrote a UUID or a name here would produce a status
		// that reads fine and does not resolve to anything in the journal.
		ur := newUndo("status-ulid")
		if err := create(t, ur); err != nil {
			t.Fatalf("create: %v", err)
		}
		ur.Status.Phase = agentv1alpha1.UndoExecuting
		ur.Status.UndoActionID = "not-a-ulid"
		if err := k8s.Status().Update(ctx, ur); err == nil {
			t.Error("status.undoActionId accepted a non-ULID; it is the join key back into the journal")
		}
		ur.Status.UndoActionID = "01JQ0000000000000000000001"
		if err := k8s.Status().Update(ctx, ur); err != nil {
			t.Errorf("a well-formed ULID was rejected: %v", err)
		}
	})

	t.Run("phase is a closed set", func(t *testing.T) {
		ur := newUndo("bad-phase")
		if err := create(t, ur); err != nil {
			t.Fatalf("create: %v", err)
		}
		ur.Status.Phase = agentv1alpha1.UndoPhase("Cancelled")
		if err := k8s.Status().Update(ctx, ur); err == nil {
			t.Error("an unknown phase was stored; IsTerminal() would report it non-terminal and the controller would retry it forever")
		}
		// The Failed/Refused distinction is load-bearing and both must exist: Failed invites a retry,
		// Refused says a retry will produce the same answer.
		for _, ok := range []agentv1alpha1.UndoPhase{
			agentv1alpha1.UndoPending, agentv1alpha1.UndoExecuting,
			agentv1alpha1.UndoExecuted, agentv1alpha1.UndoFailed, agentv1alpha1.UndoRefused,
		} {
			ur.Status.Phase = ok
			ur.Status.CompletionTime = &metav1.Time{Time: time.Unix(1770000000, 0)}
			if err := k8s.Status().Update(ctx, ur); err != nil {
				t.Errorf("phase %q was rejected: %v", ok, err)
			}
		}
	})
}
