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

package escalate_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/escalate"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// fixtureNow is fixed so a timestamp assertion is about ordering, never about wall time.
var fixtureNow = time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)

// recordFixture is a schema-valid ActionRecord. It is built to satisfy the REAL CRD -- every
// required field and every spec-level CEL rule -- because the envtest half of this suite creates it
// through a real API server, and a fixture that only satisfies the fake would make the two halves
// test different objects.
func recordFixture(actionID, ns string) *agentv1alpha1.ActionRecord {
	sum := sha256.Sum256([]byte(actionID))
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: journal.RecordName(actionID), Namespace: ns},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            actionID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "team-x-agent", Namespace: ns},
			AgentIdentity:       "developer-team/proj/cluster-a/team-x",
			ActorServiceAccount: "developer-team-team-x-actor",
			Requester:           agentv1alpha1.ActionRequester{Kind: "human", ID: "alice@example.com", Platform: "k8s"},
			Trigger:             agentv1alpha1.ActionTrigger{Source: "chat", ChainID: actionID},
			Intent:              "scale the api deployment to 5",
			IdempotencyKey:      "sha256:" + hex.EncodeToString(sum[:]),
			Classification: agentv1alpha1.ActionClassification{
				Class:    agentv1alpha1.RiskRoutine,
				Reasons:  []agentv1alpha1.ClassificationReason{{Rule: "scale", Class: "routine"}},
				Undoable: true,
			},
			Targets: []agentv1alpha1.TargetRef{{
				Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "api",
			}},
			Undo: &agentv1alpha1.UndoPlan{
				Strategy:    agentv1alpha1.UndoRestore,
				GeneratedAt: metav1.NewTime(fixtureNow.Add(-time.Minute)),
				Validated:   true,
				Steps: []agentv1alpha1.UndoStep{{
					Op:     "apply",
					Target: agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "api"},
					Object: &runtime.RawExtension{
						Raw: []byte(`{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":"api"}}`),
					},
				}},
			},
			Retention: agentv1alpha1.RetentionSpec{
				Class:               agentv1alpha1.RiskRoutine,
				TTL:                 "720h",
				ExpiresAt:           metav1.NewTime(fixtureNow.Add(720 * time.Hour)),
				UndoWindow:          "1h",
				UndoWindowExpiresAt: metav1.NewTime(fixtureNow.Add(time.Hour)),
			},
		},
	}
}

// readEscalation reads the escalation back OFF THE SERVER rather than off the object the recorder
// mutated. The recorder holds its own copy; asserting against that copy would pass even if the
// status write never left the process.
func readEscalation(t *testing.T, c client.Client, ns, name string) *agentv1alpha1.ActionEscalation {
	t.Helper()
	var live agentv1alpha1.ActionRecord
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, &live); err != nil {
		t.Fatalf("read back %s/%s: %v", ns, name, err)
	}
	if live.Status.Escalation == nil {
		t.Fatalf("%s/%s carries no escalation", ns, name)
	}
	return live.Status.Escalation
}

// The half of V-REV-006 that needs no API server: what the recorder does when it CANNOT write.
//
// Every one of these is a way rung 5 can be swallowed. That is the failure this rung exists to
// prevent, so each is asserted to surface as an error rather than as a quiet nil, and each is
// asserted to SAY which half failed -- a page that did not reach anybody and a pause that did not
// take are different incidents, and an operator reading a broker log at 3am gets one line.
//
// The properties that need a real server -- that the escalation actually lands on the record, that
// two writers do not lose each other's half, that a rung never reached leaves no escalation behind
// -- are in escalate_envtest_test.go.

func mustScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := agentv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("add scheme: %v", err)
	}
	return s
}

// TestAnUnaddressedEscalationIsRefusedBeforeItIsMisread: an escalation with no actionID has no
// destination. The failure without the guard is not a clean error -- it is a Get on the empty name,
// whose NotFound reads as "somebody deleted the record", sending whoever debugs it to look for a
// deletion that never happened.
func TestAnUnaddressedEscalationIsRefusedBeforeItIsMisread(t *testing.T) {
	var gets int
	c := fake.NewClientBuilder().WithScheme(mustScheme(t)).WithInterceptorFuncs(interceptor.Funcs{
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			gets++
			return cl.Get(ctx, key, obj, opts...)
		},
	}).Build()
	r := &escalate.Recorder{Client: c, Namespace: "team-x"}

	pageErr := r.Page(context.Background(), verify.PageRequest{AgentIdentity: "developer-team/team-x"})
	pauseErr := r.Pause(context.Background(), verify.PauseRequest{AgentIdentity: "developer-team/team-x"})

	for name, err := range map[string]error{"Page": pageErr, "Pause": pauseErr} {
		if err == nil {
			t.Fatalf("%s with no actionId returned nil: rung 5 was silently dropped", name)
		}
		if !strings.Contains(err.Error(), "actionId") {
			t.Errorf("%s error does not name the missing actionId: %v", name, err)
		}
		// The identity is the only thing left to search on once the record reference is gone.
		if !strings.Contains(err.Error(), "developer-team/team-x") {
			t.Errorf("%s error does not name the agent: %v", name, err)
		}
	}
	if gets != 0 {
		t.Errorf("the recorder issued %d Get(s) for an unaddressed escalation; it must refuse before it asks", gets)
	}
}

// TestAMissingRecordIsAnErrorThatSaysWhatItCosts: the record is the only surface the broker can
// escalate on. If it is gone, nothing downstream will ever pause the agent -- so the error has to
// say that, not just "not found".
func TestAMissingRecordIsAnErrorThatSaysWhatItCosts(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(mustScheme(t)).Build()
	r := &escalate.Recorder{Client: c, Namespace: "team-x"}

	err := r.Pause(context.Background(), verify.PauseRequest{
		ActionID: "01JQ0000000000000000000000", AgentIdentity: "developer-team/team-x", Reason: "restore failed",
	})
	if err == nil {
		t.Fatal("escalating onto a record that does not exist returned nil")
	}
	if !strings.Contains(err.Error(), "stays live") {
		t.Errorf("the error does not say the agent stays live and nobody is paged: %v", err)
	}
	// The derived object name, not the raw actionID: an uppercase ULID is not a legal object name,
	// so an error quoting the raw ID would send somebody looking for an object that could not exist.
	if !strings.Contains(err.Error(), "ar-01jq0000000000000000000000") {
		t.Errorf("the error does not name the record it looked for: %v", err)
	}
}

// TestAFailedStatusWriteIsNeverSwallowed. A conflict here is the interesting case and it is
// deliberately NOT retried: the driver is on a one-attempt path, and 04 §5.1 says a failed rollback
// is "an immediate page, not a retry loop". A retry loop hidden inside the recorder would be that
// loop wearing a different hat, and it would delay the one write that gets a human involved.
func TestAFailedStatusWriteIsNeverSwallowed(t *testing.T) {
	boom := errors.New("etcd is unavailable")
	var updates int
	c := fake.NewClientBuilder().
		WithScheme(mustScheme(t)).
		WithObjects(recordFixture("01JQ0000000000000000000000", "team-x")).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithInterceptorFuncs(interceptor.Funcs{
			SubResourceUpdate: func(context.Context, client.Client, string, client.Object, ...client.SubResourceUpdateOption) error {
				updates++
				return boom
			},
		}).Build()
	r := &escalate.Recorder{Client: c, Namespace: "team-x"}

	err := r.Page(context.Background(), verify.PageRequest{
		ActionID: "01JQ0000000000000000000000", AgentIdentity: "developer-team/team-x",
		Summary: "rollback failed", RollbackError: "the API server rejected the restore",
	})
	if err == nil {
		t.Fatal("a status write that failed was reported as a delivered escalation")
	}
	if !errors.Is(err, boom) {
		t.Errorf("the underlying cause was dropped: %v", err)
	}
	if updates != 1 {
		t.Errorf("the recorder attempted %d status writes; 04 §5.1 forbids a retry loop here", updates)
	}
}

// TestTheReasonIsTruncatedRatherThanLosingTheBrake. `reason` is bounded at 512 by the schema, and a
// rollback error can carry a whole API-server validation message. Failing the escalation because the
// diagnostic was long would trade the brake for the diagnostic; the diagnostic is the cheaper loss.
func TestTheReasonIsTruncatedRatherThanLosingTheBrake(t *testing.T) {
	c := fake.NewClientBuilder().
		WithScheme(mustScheme(t)).
		WithObjects(recordFixture("01JQ0000000000000000000000", "team-x")).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		Build()
	r := &escalate.Recorder{Client: c, Namespace: "team-x"}

	// A multi-byte tail, so a byte-wise cut would land mid-rune and produce a replacement glyph in
	// the middle of an incident message.
	long := "rollback failed after the deployment never converged: " + strings.Repeat("é", 800)
	if err := r.Pause(context.Background(), verify.PauseRequest{
		ActionID: "01JQ0000000000000000000000", AgentIdentity: "developer-team/team-x", Reason: long,
	}); err != nil {
		t.Fatalf("a long reason failed the escalation instead of being truncated: %v", err)
	}

	got := readEscalation(t, c, "team-x", "ar-01jq0000000000000000000000")
	if n := len([]rune(got.Reason)); n > 512 {
		t.Errorf("reason is %d runes; the schema bound is 512 and the API server would reject it", n)
	}
	if !strings.HasPrefix(got.Reason, "rollback failed after the deployment never converged") {
		t.Errorf("truncation cut the front, which carries the classification: %q", got.Reason)
	}
	if strings.Contains(got.Reason, "�") {
		t.Errorf("truncation split a multi-byte rune: %q", got.Reason)
	}
	if !got.PauseRequested {
		t.Error("the pause was not requested; truncation must not cost the brake")
	}
}
