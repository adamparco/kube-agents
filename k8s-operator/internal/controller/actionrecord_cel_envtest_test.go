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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The ActionRecord CEL rules are the ones a code review cannot confirm by reading. controller-gen
// will happily emit a rule the API server then rejects at CRD-install time (the schema is valid; the
// expression is not), and a rule that installs may still never fire. Both failures are silent: the
// CRD is Established, records are accepted, and the journal's immutability is a comment.
//
// So every rule in actionrecord_types.go is exercised here against a real API server, in BOTH
// directions -- a valid record must be accepted and the violating one rejected. A test that only
// checks the happy path would pass against a CRD with the validations deleted.
//
// V-BRK-003 (journal spec immutable), V-BRK-015 (undo linkage and retention clocks).

const (
	testULID = "01JZQ8X9K7M4N2P6R8T0V3W5YZ"
	testName = "ar-01jzq8x9k7m4n2p6r8t0v3w5yz"
)

func testDigest() string { return strings.Repeat("a", 64) }

// newActionRecord builds a minimal record that satisfies every rule. Each subtest below takes this
// and breaks exactly one thing, so a rejection can only be attributed to the thing it broke.
func newActionRecord(name string, now time.Time) *agentv1alpha1.ActionRecord {
	submitted := metav1.NewTime(now)
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default"},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            testULID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "cluster-a-agent", Namespace: "default"},
			AgentIdentity:       "cluster-admin/proj-x/cluster-a",
			ActorServiceAccount: "system:serviceaccount:default:cluster-a-agent-actor",
			Requester: agentv1alpha1.ActionRequester{
				Kind: "human",
				ID:   "slack:U02ABCDEF",
			},
			Trigger: agentv1alpha1.ActionTrigger{
				Source:  "chat",
				ChainID: testULID,
			},
			Intent:         "restart the crashlooping api-gateway deployment",
			IdempotencyKey: "sha256:" + testDigest(),
			DryRun:         true,
			Classification: agentv1alpha1.ActionClassification{
				Class:    agentv1alpha1.RiskRoutine,
				Undoable: true,
			},
			Targets: []agentv1alpha1.TargetRef{{
				Group: "apps", Version: "v1", Kind: "Deployment",
				Namespace: "team-x", Name: "api-gateway",
			}},
			Retention: agentv1alpha1.RetentionSpec{
				Class:               agentv1alpha1.RiskRoutine,
				TTL:                 "720h",
				ExpiresAt:           metav1.NewTime(submitted.Add(720 * time.Hour)),
				UndoWindow:          "168h",
				UndoWindowExpiresAt: metav1.NewTime(submitted.Add(168 * time.Hour)),
			},
		},
	}
}

func TestActionRecordCEL(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the ActionRecord CEL rules")
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}

	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	// A CRD carrying an unparseable CEL expression fails HERE, at install, not at first write.
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest (a CEL compile error in the ActionRecord CRD surfaces as a CRD-install failure): %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	ctx := context.Background()
	now := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

	// create is the positive half of every pair: it names the record uniquely so subtests do not
	// collide, applies the caller's mutation, and reports whether the API server took it.
	create := func(t *testing.T, name string, mutate func(*agentv1alpha1.ActionRecord)) error {
		t.Helper()
		ar := newActionRecord(name, now)
		if mutate != nil {
			mutate(ar)
		}
		err := k8s.Create(ctx, ar)
		if err == nil {
			t.Cleanup(func() { _ = k8s.Delete(ctx, ar) })
		}
		return err
	}

	t.Run("valid record is accepted", func(t *testing.T) {
		if err := create(t, testName, nil); err != nil {
			t.Fatalf("a fully valid ActionRecord was rejected: %v", err)
		}
	})

	t.Run("spec is immutable", func(t *testing.T) {
		ar := newActionRecord("ar-immutable", now)
		if err := k8s.Create(ctx, ar); err != nil {
			t.Fatalf("create: %v", err)
		}
		t.Cleanup(func() { _ = k8s.Delete(ctx, ar) })

		// The actor SA cannot edit its own journal entry. Anything in spec: the intent is the field
		// an agent would most want to rewrite after the fact.
		ar.Spec.Intent = "something entirely different"
		if err := k8s.Update(ctx, ar); err == nil {
			t.Fatal("spec.intent was mutated after creation; the journal is not append-only (V-BRK-003)")
		} else if !strings.Contains(err.Error(), "immutable") {
			t.Fatalf("spec mutation was rejected, but not by the immutability rule: %v", err)
		}
	})

	t.Run("status is writable while spec is frozen", func(t *testing.T) {
		// The immutability rule must not also freeze status: a spec-level `self == oldSelf` that
		// somehow applied to the whole object would deadlock the broker, and the symptom would be a
		// journal permanently stuck in Pending rather than an obvious error.
		ar := newActionRecord("ar-status-writable", now)
		if err := k8s.Create(ctx, ar); err != nil {
			t.Fatalf("create: %v", err)
		}
		t.Cleanup(func() { _ = k8s.Delete(ctx, ar) })

		ar.Status.Phase = agentv1alpha1.PhaseDryRun
		ar.Status.Message = "classified, planned, journaled, not executed"
		if err := k8s.Status().Update(ctx, ar); err != nil {
			t.Fatalf("status update was rejected; the spec immutability rule is over-broad: %v", err)
		}

		var got agentv1alpha1.ActionRecord
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(ar), &got); err != nil {
			t.Fatalf("get: %v", err)
		}
		if got.Status.Phase != agentv1alpha1.PhaseDryRun {
			t.Fatalf("status.phase = %q, want %q (status subresource not enabled?)", got.Status.Phase, agentv1alpha1.PhaseDryRun)
		}
	})

	t.Run("undo linkage: undoOf required when source is undo", func(t *testing.T) {
		err := create(t, "ar-undo-missing-undoof", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Trigger.Source = agentv1alpha1.ActionTriggerUndo
		})
		if err == nil {
			t.Fatal("an undo-sourced record with no spec.trigger.undoOf was accepted; the undo it reverses is unrecoverable (V-BRK-015)")
		}
	})

	t.Run("undo linkage: undoOf forbidden when source is not undo", func(t *testing.T) {
		err := create(t, "ar-chat-with-undoof", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Trigger.UndoOf = testULID
		})
		if err == nil {
			t.Fatal("a chat-sourced record claimed to undo another action; the linkage would be a lie (V-BRK-015)")
		}
	})

	t.Run("undo linkage: undo-sourced record with undoOf is accepted", func(t *testing.T) {
		if err := create(t, "ar-undo-valid", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Trigger.Source = agentv1alpha1.ActionTriggerUndo
			ar.Spec.Trigger.UndoOf = testULID
		}); err != nil {
			t.Fatalf("a correctly linked undo record was rejected: %v", err)
		}
	})

	t.Run("retention: the undo promise may not outlive the record", func(t *testing.T) {
		err := create(t, "ar-undo-window-too-long", func(ar *agentv1alpha1.ActionRecord) {
			// 90-day undo window on a 30-day record: the promise survives the evidence.
			ar.Spec.Retention.UndoWindow = "2160h"
			ar.Spec.Retention.UndoWindowExpiresAt = metav1.NewTime(now.Add(2160 * time.Hour))
		})
		if err == nil {
			t.Fatal("undoWindowExpiresAt after expiresAt was accepted; undo would be promised past the record's own deletion (V-BRK-015)")
		}
	})

	t.Run("preState: neither object nor objectRef is rejected", func(t *testing.T) {
		err := create(t, "ar-prestate-empty", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.PreState = []agentv1alpha1.PreStateSnapshot{{
				TargetIndex: 0,
				CapturedAt:  metav1.NewTime(now),
				SHA256:      testDigest(),
			}}
		})
		if err == nil {
			t.Fatal("a preState entry with no body and no objectRef was accepted; the record claims an undoable snapshot it does not have")
		}
	})

	t.Run("preState: both object and objectRef is rejected", func(t *testing.T) {
		err := create(t, "ar-prestate-both", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.PreState = []agentv1alpha1.PreStateSnapshot{{
				TargetIndex: 0,
				CapturedAt:  metav1.NewTime(now),
				Object:      &runtime.RawExtension{Raw: []byte(`{"kind":"Deployment"}`)},
				ObjectRef: &agentv1alpha1.ObjectStoreRef{
					Store: "journal", Key: "k", SHA256: testDigest(),
				},
				SHA256: testDigest(),
			}}
		})
		if err == nil {
			t.Fatal("a preState entry carrying both an inline body and an objectRef was accepted; undo would have two sources of truth")
		}
	})

	t.Run("preState: inline body is accepted", func(t *testing.T) {
		if err := create(t, "ar-prestate-inline", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.PreState = []agentv1alpha1.PreStateSnapshot{{
				TargetIndex: 0,
				CapturedAt:  metav1.NewTime(now),
				Object:      &runtime.RawExtension{Raw: []byte(`{"kind":"Deployment","spec":{"replicas":3}}`)},
				SHA256:      testDigest(),
			}}
		}); err != nil {
			t.Fatalf("an inline preState snapshot was rejected: %v", err)
		}
	})

	t.Run("preState: objectRef body is accepted", func(t *testing.T) {
		if err := create(t, "ar-prestate-ref", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.PreState = []agentv1alpha1.PreStateSnapshot{{
				TargetIndex: 0,
				CapturedAt:  metav1.NewTime(now),
				ObjectRef: &agentv1alpha1.ObjectStoreRef{
					Store: "journal", Key: "proj-x/cluster-a/" + testULID + "/0", SHA256: testDigest(),
				},
				SHA256: testDigest(),
			}}
		}); err != nil {
			t.Fatalf("an out-of-band preState snapshot was rejected: %v", err)
		}
	})

	t.Run("undo plan: a strategy other than none must carry steps", func(t *testing.T) {
		err := create(t, "ar-undo-plan-empty", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Undo = &agentv1alpha1.UndoPlan{
				Strategy:    agentv1alpha1.UndoRestore,
				GeneratedAt: metav1.NewTime(now),
				Validated:   true,
			}
		})
		if err == nil {
			t.Fatal("an undo plan claimed strategy=restore with no steps; the record advertises an undo that would do nothing")
		}
	})

	t.Run("undo plan: strategy none needs no steps", func(t *testing.T) {
		if err := create(t, "ar-undo-plan-none", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Undo = &agentv1alpha1.UndoPlan{
				Strategy:    agentv1alpha1.UndoNone,
				GeneratedAt: metav1.NewTime(now),
				Caveats:     []string{"no safe inverse for this operation"},
			}
			// No safe inverse forces the classification to at least gated (06 §4.3.1).
			ar.Spec.Classification.Undoable = false
			ar.Spec.Classification.Class = agentv1alpha1.RiskGated
		}); err != nil {
			t.Fatalf("an honest strategy=none plan was rejected: %v", err)
		}
	})

	t.Run("actionId must be a ULID", func(t *testing.T) {
		// Crockford base32 excludes I, L, O and U precisely so a transcribed identifier cannot be
		// misread. A lowercase or short id here would still round-trip through the label, where it
		// is lower-cased, and silently break the join back to the record.
		for _, bad := range []string{
			strings.ToLower(testULID),
			testULID[:25],
			"01JZQ8X9K7M4N2P6R8T0V3W5YI", // I is not in the alphabet
		} {
			if err := create(t, "ar-bad-ulid", func(ar *agentv1alpha1.ActionRecord) {
				ar.Spec.ActionID = bad
			}); err == nil {
				t.Fatalf("spec.actionId = %q was accepted; it is not a ULID", bad)
			}
		}
	})

	t.Run("idempotencyKey must be a sha256 digest", func(t *testing.T) {
		if err := create(t, "ar-bad-idempotency", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.IdempotencyKey = "sha256:not-a-digest"
		}); err == nil {
			t.Fatal("a malformed idempotencyKey was accepted; duplicate suppression keys off this value")
		}
	})

	t.Run("at least one target is required", func(t *testing.T) {
		if err := create(t, "ar-no-targets", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Targets = nil
		}); err == nil {
			t.Fatal("a record with no targets was accepted; there is nothing to snapshot or undo")
		}
	})

	t.Run("risk class is a closed enum", func(t *testing.T) {
		if err := create(t, "ar-bad-class", func(ar *agentv1alpha1.ActionRecord) {
			ar.Spec.Classification.Class = agentv1alpha1.ActionRiskClass("low")
		}); err == nil {
			t.Fatal("classification.class = low was accepted; the classifier's output is a closed set (06 §4.2)")
		}
	})

	t.Run("phase is a closed enum", func(t *testing.T) {
		ar := newActionRecord("ar-bad-phase", now)
		if err := k8s.Create(ctx, ar); err != nil {
			t.Fatalf("create: %v", err)
		}
		t.Cleanup(func() { _ = k8s.Delete(ctx, ar) })

		ar.Status.Phase = agentv1alpha1.ActionPhase("Done")
		if err := k8s.Status().Update(ctx, ar); err == nil {
			t.Fatal("status.phase = Done was accepted; the ten-phase lifecycle is a closed set (06 §4.3)")
		}
	})
}
