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
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// vap-agent-scope-journal is a table in 06 §4.3 rendered as CEL, and a table rendered as CEL has two
// ways to be wrong that reading cannot catch: an expression that does not compile (the policy is
// created, `failurePolicy: Fail` is set, and nothing is enforced because nothing loaded), and an
// expression that compiles into the wrong predicate (allows what it should deny). Both leave a
// cluster that looks configured.
//
// So the table is exercised row by row, against a real API server, with real impersonation, in both
// directions. Every principal gets at least one write it MAY make and one it may NOT, because a
// policy that denies everything passes any test that only checks denials -- and it would also break
// the broker.
//
// The row with no permitted write at all is the human cluster-admin, and it is the reason the policy
// exists: without it any cluster-admin could mark their own gated action granted and execute it, and
// four-eyes would be decorative.

const (
	testNS = "default"
	// The record's own declared actor SA. The policy identifies the owning broker by matching this
	// in the record's namespace, so the two must agree for the broker rows to be exercised at all.
	brokerSA    = "cluster-admin-cluster-a-actor"
	brokerUser  = "system:serviceaccount:" + testNS + ":" + brokerSA
	otherBroker = "system:serviceaccount:" + testNS + ":cluster-admin-cluster-b-actor"
	undoUser    = "system:serviceaccount:kubeagents-system:kube-agents-undo-controller"
	brakeUser   = "system:serviceaccount:kubeagents-system:kube-agents-brake-controller"
	chatOpsUser = "system:serviceaccount:kubeagents-system:kube-agents-chatops-gateway"
	exportUser  = "system:serviceaccount:kubeagents-system:kubeagents-controller"
	retainUser  = "system:serviceaccount:kubeagents-system:kube-agents-retention-controller"
	humanUser   = "alice@example.com"

	// testWriterGroup carries the RBAC. Impersonation only decides WHO the request is from; the
	// impersonated principal still needs permission, and without this every case below would fail
	// with a 403 from RBAC and prove nothing about the policy.
	testWriterGroup = "kube-agents-journal-test-writers"
)

func TestJournalStatusPolicy(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise vap-agent-scope-journal")
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
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest: %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new admin client: %v", err)
	}
	ctx := context.Background()

	grantTestWriters(ctx, t, admin)
	// A CEL compile error in the policy surfaces here, as an apply failure or a policy that never
	// starts enforcing -- which waitForPolicy turns into a test failure rather than a silent pass.
	applyPolicyFile(ctx, t, admin, filepath.Join("..", "..", "config", "policy", "vap-agent-scope-journal.yaml"))

	as := func(user string) client.Client {
		impersonated := rest.CopyConfig(cfg)
		impersonated.Impersonate = rest.ImpersonationConfig{
			UserName: user,
			Groups:   []string{testWriterGroup},
		}
		c, cErr := client.New(impersonated, client.Options{Scheme: scheme})
		if cErr != nil {
			t.Fatalf("impersonated client for %q: %v", user, cErr)
		}
		return c
	}

	now := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	seq := 0
	// mkRecord creates a fresh record as admin (admin is not subject to the status policy on
	// CREATE, which only matches UPDATE of the status subresource).
	mkRecord := func(t *testing.T) *agentv1alpha1.ActionRecord {
		t.Helper()
		seq++
		ar := newActionRecord(fmt.Sprintf("ar-policy-%02d", seq), now)
		ar.Spec.ActorServiceAccount = brokerSA
		// The action id must stay unique per record: it is a ULID pattern, so vary the last
		// character rather than the shape.
		ar.Spec.ActionID = fmt.Sprintf("%s%02d", testULID[:24], seq%100)
		if err := admin.Create(ctx, ar); err != nil {
			t.Fatalf("create record: %v", err)
		}
		return ar
	}

	// The policy is loaded asynchronously by the API server's policy controller. Poll on a write
	// the table forbids until it is actually forbidden; otherwise every "allowed" case below would
	// pass simply because nothing was enforcing yet.
	waitForPolicy(ctx, t, as(humanUser), mkRecord(t))

	// statusUpdate applies a mutation to a fresh record's status as the given principal.
	statusUpdate := func(t *testing.T, user string, mutate func(*agentv1alpha1.ActionRecord)) error {
		t.Helper()
		ar := mkRecord(t)
		mutate(ar)
		return as(user).Status().Update(ctx, ar)
	}

	// --- the owning broker -------------------------------------------------------------------

	t.Run("broker may write phase, report and timestamps", func(t *testing.T) {
		err := statusUpdate(t, brokerUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseDryRun
			ar.Status.ObservedGeneration = 1
			ar.Status.Message = "classified, planned, journaled, not executed"
			ar.Status.Report = &agentv1alpha1.ActionReport{
				Noticed: "api-gateway is crashlooping",
				Did:     "nothing -- shadow mode",
				Undo:    "n/a",
			}
			ar.Status.Timestamps = &agentv1alpha1.ActionTimestamps{Submitted: ptrTime(now)}
		})
		if err != nil {
			t.Fatalf("the owning broker was denied its own fields: %v", err)
		}
	})

	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.ActionRecord)
	}{
		{"approvals", func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
				Required: 1,
				Granted:  []agentv1alpha1.ApprovalEntry{{Principal: brokerUser, At: metav1.NewTime(now)}},
			}
		}},
		{"contested", func(ar *agentv1alpha1.ActionRecord) { ar.Status.Contested = true }},
		{"undoneBy", func(ar *agentv1alpha1.ActionRecord) { ar.Status.UndoneBy = testULID }},
		{"exported", func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Exported = &agentv1alpha1.ExportStatus{Confirmed: true, Sink: "memory"}
		}},
	} {
		t.Run("broker may not write "+tc.name, func(t *testing.T) {
			if err := statusUpdate(t, brokerUser, tc.mutate); err == nil {
				t.Fatalf("the owning broker wrote status.%s; an actor that can write it can vouch for its own action (06 §4.3)", tc.name)
			} else if !apierrors.IsForbidden(err) {
				t.Fatalf("expected Forbidden writing status.%s, got: %v", tc.name, err)
			}
		})
	}

	// --- rung 5 (04 §5.1): who may ask for a page and a pause, and who may say they happened -----
	//
	// `status.escalation` is the seam between the broker and C-BR, and it is the only surface on
	// which the broker can escalate at all: 06 §2.2.1 gives it no verb on `events` and no write on
	// `agents`, so it cannot page and cannot pause. It records the REQUEST; C-BR performs it.
	//
	// This is also the row that exposed how this policy fails. Its allow-list is an enumeration of
	// status fields, and validation 1 admits any write for which `nothingChanged` holds -- so before
	// `escalationChanged` was added to that conjunction, an UPDATE touching only `status.escalation`
	// was invisible to it and admitted from EVERY principal, human cluster-admin included. Delete
	// `!variables.escalationChanged` from `nothingChanged` and the two denial cases below go green
	// in the wrong direction; dev/tests/journal-status-vap-parity.py is what stops the next status
	// field arriving with the same hole.

	t.Run("broker may request a page and a pause", func(t *testing.T) {
		err := statusUpdate(t, brokerUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Escalation = &agentv1alpha1.ActionEscalation{
				PageRequested:  true,
				PauseRequested: true,
				Reason:         "rollback failed after the deployment never converged",
				RequestedAt:    ptrTime(now),
			}
		})
		if err != nil {
			t.Fatalf("the owning broker was denied rung 5: a failed rollback would then reach nobody (04 §5.1): %v", err)
		}
	})

	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.ActionEscalation)
	}{
		{"pagedAt", func(e *agentv1alpha1.ActionEscalation) { e.PagedAt = ptrTime(now) }},
		{"pausedAt", func(e *agentv1alpha1.ActionEscalation) { e.PausedAt = ptrTime(now) }},
		{"failure", func(e *agentv1alpha1.ActionEscalation) { e.Failure = "C-BR could not reach the Agent" }},
	} {
		t.Run("broker may not record escalation."+tc.name, func(t *testing.T) {
			err := statusUpdate(t, brokerUser, func(ar *agentv1alpha1.ActionRecord) {
				ar.Status.Escalation = &agentv1alpha1.ActionEscalation{
					PageRequested: true, PauseRequested: true, RequestedAt: ptrTime(now),
				}
				tc.mutate(ar.Status.Escalation)
			})
			if err == nil {
				t.Fatalf("the broker recorded escalation.%s -- an outcome it has no verb to produce. "+
					"A self-attested page is worse than no record: the audit trail then asserts the "+
					"promise was kept (04 §5.1, 05 §1.7)", tc.name)
			} else if !apierrors.IsForbidden(err) {
				t.Fatalf("expected Forbidden writing escalation.%s, got: %v", tc.name, err)
			}
		})
	}

	// --- C-BR, the other half of rung 5 --------------------------------------------------------
	//
	// The broker's denial above is only half a seam. If C-BR could write the request it fulfils, one
	// compromised controller could author an escalation and act on it, and the two-writer split would
	// buy nothing; if the broker could ERASE C-BR's receipt, a failed pause could be laundered back
	// into a pending one. Both directions are exercised here.

	// escalated returns a record already carrying a broker-written request, which is the only state
	// in which C-BR is allowed to write anything at all.
	escalated := func(t *testing.T) *agentv1alpha1.ActionRecord {
		t.Helper()
		ar := mkRecord(t)
		ar.Status.Escalation = &agentv1alpha1.ActionEscalation{
			PageRequested:  true,
			PauseRequested: true,
			Reason:         "rollback failed after the deployment never converged",
			RequestedAt:    ptrTime(now),
		}
		if err := as(brokerUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("broker could not request rung 5: %v", err)
		}
		return ar
	}

	t.Run("C-BR may record that the page and the pause happened", func(t *testing.T) {
		ar := escalated(t)
		ar.Status.Escalation.PagedAt = ptrTime(now)
		ar.Status.Escalation.PausedAt = ptrTime(now)
		if err := as(brakeUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("C-BR was denied its own fields; the fan-out would then be unauditable (04 §5.1): %v", err)
		}
	})

	t.Run("C-BR may record a fan-out failure", func(t *testing.T) {
		ar := escalated(t)
		ar.Status.Escalation.Failure = "agent not found"
		if err := as(brakeUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("C-BR was denied escalation.failure; a fan-out that could not be recorded as failed "+
				"is indistinguishable from one still pending: %v", err)
		}
	})

	t.Run("C-BR may not create the escalation it fulfils", func(t *testing.T) {
		err := statusUpdate(t, brakeUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Escalation = &agentv1alpha1.ActionEscalation{
				PauseRequested: true,
				Reason:         "because I said so",
				RequestedAt:    ptrTime(now),
				PausedAt:       ptrTime(now),
			}
		})
		if err == nil {
			t.Fatal("C-BR wrote itself an escalation and fulfilled it in the same breath; the controller " +
				"that holds the pause verb must never author the justification for using it (05 §1.7)")
		} else if !apierrors.IsForbidden(err) {
			t.Fatalf("expected Forbidden creating an escalation as C-BR, got: %v", err)
		}
	})

	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.ActionEscalation)
	}{
		{"reason", func(e *agentv1alpha1.ActionEscalation) { e.Reason = "a reason the pause looks warranted by" }},
		{"requestedAt", func(e *agentv1alpha1.ActionEscalation) { e.RequestedAt = ptrTime(now.Add(-time.Hour)) }},
		{"pauseRequested", func(e *agentv1alpha1.ActionEscalation) { e.PauseRequested = false }},
	} {
		t.Run("C-BR may not rewrite escalation."+tc.name, func(t *testing.T) {
			ar := escalated(t)
			tc.mutate(ar.Status.Escalation)
			ar.Status.Escalation.PagedAt = ptrTime(now)
			err := as(brakeUser).Status().Update(ctx, ar)
			if err == nil {
				t.Fatalf("C-BR rewrote escalation.%s -- the request is the broker's observation of a "+
					"failure C-BR never saw (04 §5.1)", tc.name)
			} else if !apierrors.IsForbidden(err) {
				t.Fatalf("expected Forbidden rewriting escalation.%s, got: %v", tc.name, err)
			}
		})
	}

	t.Run("C-BR may not write any other status field", func(t *testing.T) {
		ar := escalated(t)
		ar.Status.Escalation.PausedAt = ptrTime(now)
		ar.Status.Phase = agentv1alpha1.PhaseUndone
		err := as(brakeUser).Status().Update(ctx, ar)
		if err == nil {
			t.Fatal("C-BR moved the record's phase; it fans out an escalation and describes nothing else (06 §4.3)")
		} else if !apierrors.IsForbidden(err) {
			t.Fatalf("expected Forbidden writing status.phase as C-BR, got: %v", err)
		}
	})

	// The mirror of the fail-open this policy already paid for once: asking whether the NEW object
	// carries a fulfilment field answers "no" when the field is being deleted. Deletion is the
	// interesting direction -- an erased pausedAt reads as a fan-out still in flight rather than one
	// that happened, so the broker would be able to retract C-BR's receipt.
	t.Run("broker may not erase C-BR's receipt", func(t *testing.T) {
		ar := escalated(t)
		ar.Status.Escalation.PagedAt = ptrTime(now)
		ar.Status.Escalation.PausedAt = ptrTime(now)
		if err := as(brakeUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("C-BR could not record the fan-out: %v", err)
		}
		ar.Status.Escalation.PagedAt = nil
		ar.Status.Escalation.PausedAt = nil
		err := as(brokerUser).Status().Update(ctx, ar)
		if err == nil {
			t.Fatal("the broker deleted escalation.pagedAt and pausedAt; a receipt that the party being " +
				"audited can retract is not a receipt (04 §5.1)")
		} else if !apierrors.IsForbidden(err) {
			t.Fatalf("expected Forbidden erasing the fulfilment half, got: %v", err)
		}
	})

	t.Run("a different broker may not write this record", func(t *testing.T) {
		err := statusUpdate(t, otherBroker, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseVerified
		})
		if err == nil {
			t.Fatal("cluster-b's broker wrote status on cluster-a's record; the owning-broker constraint is not enforced (06 §4.3)")
		}
	})

	// --- the undo controller -----------------------------------------------------------------

	t.Run("undo controller may set Undone, undoneBy and contested", func(t *testing.T) {
		err := statusUpdate(t, undoUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseUndone
			ar.Status.UndoneBy = testULID
			ar.Status.Contested = true
			ar.Status.Message = "undone by a human"
		})
		if err != nil {
			t.Fatalf("the undo controller was denied its own fields: %v", err)
		}
	})

	t.Run("undo controller may not move a record to Verified", func(t *testing.T) {
		err := statusUpdate(t, undoUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseVerified
		})
		if err == nil {
			t.Fatal("the undo controller moved a record to Verified; it may only move records to Undone (06 §4.3)")
		}
	})

	t.Run("undo controller may not write verification", func(t *testing.T) {
		err := statusUpdate(t, undoUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Verification = &agentv1alpha1.ActionVerification{Passed: true}
		})
		if err == nil {
			t.Fatal("the undo controller described what the action did; that is the broker's field (06 §4.3)")
		}
	})

	// --- the ChatOps gateway -----------------------------------------------------------------

	t.Run("gateway may record approvals", func(t *testing.T) {
		err := statusUpdate(t, chatOpsUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
				Required: 2,
				Granted: []agentv1alpha1.ApprovalEntry{
					{Principal: "slack:U0A", At: metav1.NewTime(now)},
					{Principal: "slack:U0B", At: metav1.NewTime(now)},
				},
			}
		})
		if err != nil {
			t.Fatalf("the ChatOps gateway was denied status.approvals: %v", err)
		}
	})

	t.Run("gateway may move PendingApproval to Rejected", func(t *testing.T) {
		ar := mkRecord(t)
		// Get the record into PendingApproval first, as the broker, which is how it happens.
		ar.Status.Phase = agentv1alpha1.PhasePendingApproval
		if err := as(brokerUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("broker could not set PendingApproval: %v", err)
		}
		ar.Status.Phase = agentv1alpha1.PhaseRejected
		if err := as(chatOpsUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("the gateway was denied the PendingApproval -> Rejected transition: %v", err)
		}
	})

	t.Run("gateway may not move Pending straight to Verified", func(t *testing.T) {
		err := statusUpdate(t, chatOpsUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseVerified
		})
		if err == nil {
			t.Fatal("the gateway executed an action by declaring it Verified; it may only resolve PendingApproval (06 §4.3)")
		}
	})

	t.Run("gateway may clear contested but not set it", func(t *testing.T) {
		ar := mkRecord(t)
		ar.Status.Contested = true
		if err := as(undoUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("undo controller could not set contested: %v", err)
		}
		ar.Status.Contested = false
		if err := as(chatOpsUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("the gateway was denied /kage uncontest: %v", err)
		}

		if err := statusUpdate(t, chatOpsUser, func(a *agentv1alpha1.ActionRecord) { a.Status.Contested = true }); err == nil {
			t.Fatal("the gateway SET contested; contested is an observation that an undo happened, not a declaration (06 §4.3)")
		}
	})

	// --- the exporter ------------------------------------------------------------------------

	t.Run("exporter may write exported and nothing else", func(t *testing.T) {
		if err := statusUpdate(t, exportUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Exported = &agentv1alpha1.ExportStatus{Confirmed: true, Sink: "memory", At: ptrTime(now)}
		}); err != nil {
			t.Fatalf("the exporter was denied status.exported: %v", err)
		}
		if err := statusUpdate(t, exportUser, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseVerified
		}); err == nil {
			t.Fatal("the exporter wrote status.phase; the principal that unlocks deletion may write only its own confirmation (06 §4.3)")
		}
	})

	// --- everyone else is out of the escalation row ---------------------------------------------
	//
	// Only the owning broker asks for rung 5. The exporter is listed because it is the same service
	// account C-BR will run under (both live in the operator manager) and is therefore the row most
	// likely to be widened by accident when C-BR lands -- and the exporter is the one principal
	// whose write unlocks deletion, so anything it can reach is a field that can be rewritten
	// immediately before the record is destroyed.
	for _, user := range []struct{ label, name string }{
		{"the undo controller", undoUser},
		{"the ChatOps gateway", chatOpsUser},
		{"the exporter", exportUser},
	} {
		t.Run(user.label+" may not write escalation", func(t *testing.T) {
			err := statusUpdate(t, user.name, func(ar *agentv1alpha1.ActionRecord) {
				ar.Status.Escalation = &agentv1alpha1.ActionEscalation{
					PauseRequested: true, Reason: "because I said so", RequestedAt: ptrTime(now),
				}
			})
			if err == nil {
				t.Fatalf("%s requested an auto-pause; once C-BR fans this out that is a stop button "+
					"for any agent, held by a principal 06 §4.3 never gave it to", user.label)
			}
		})
	}

	// --- the human -----------------------------------------------------------------------------

	t.Run("a human may not write any status field", func(t *testing.T) {
		for _, tc := range []struct {
			name   string
			mutate func(*agentv1alpha1.ActionRecord)
		}{
			{"phase", func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseVerified }},
			// The field that was writable by ANYONE until `escalationChanged` joined the
			// `nothingChanged` conjunction. A human who can request a pause can stop any agent in
			// the namespace without going through /kage, which is the whole shape 06 §4.3 forbids.
			{"escalation", func(ar *agentv1alpha1.ActionRecord) {
				ar.Status.Escalation = &agentv1alpha1.ActionEscalation{
					PauseRequested: true, Reason: "stop", RequestedAt: ptrTime(now),
				}
			}},
			{"approvals", func(ar *agentv1alpha1.ActionRecord) {
				ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
					Required: 1,
					Granted:  []agentv1alpha1.ApprovalEntry{{Principal: humanUser, At: metav1.NewTime(now)}},
				}
			}},
			{"message", func(ar *agentv1alpha1.ActionRecord) { ar.Status.Message = "looks fine to me" }},
		} {
			if err := statusUpdate(t, humanUser, tc.mutate); err == nil {
				t.Fatalf("a human wrote status.%s by hand; approving your own gated action makes four-eyes decorative (06 §4.3)", tc.name)
			}
		}
	})

	// --- deletion ------------------------------------------------------------------------------

	t.Run("an agent identity may not delete a record", func(t *testing.T) {
		ar := mkRecord(t)
		if err := as(brokerUser).Delete(ctx, ar); err == nil {
			t.Fatal("the actor SA deleted its own journal entry; the journal is append-only (06 §2.2.1)")
		}
	})

	t.Run("retention may not delete an unexported record", func(t *testing.T) {
		ar := mkRecord(t)
		if err := as(retainUser).Delete(ctx, ar); err == nil {
			t.Fatal("an unexported record was deleted; the export is the durable record, so this is data loss and not garbage collection (05 §1.2)")
		}
	})

	t.Run("retention may delete an exported record", func(t *testing.T) {
		ar := mkRecord(t)
		ar.Status.Exported = &agentv1alpha1.ExportStatus{Confirmed: true, Sink: "memory", At: ptrTime(now)}
		if err := as(exportUser).Status().Update(ctx, ar); err != nil {
			t.Fatalf("exporter could not confirm: %v", err)
		}
		if err := as(retainUser).Delete(ctx, ar); err != nil {
			t.Fatalf("retention was denied a confirmed-exported record; the controller could never garbage-collect: %v", err)
		}
	})
}

func ptrTime(t time.Time) *metav1.Time {
	mt := metav1.NewTime(t.UTC())
	return &mt
}

// grantTestWriters gives every impersonated principal full access to actionrecords, so that a
// denial in this test can only have come from the policy. Without it, RBAC would deny first and the
// test would be green for the wrong reason -- the most common way an admission-policy test lies.
func grantTestWriters(ctx context.Context, t *testing.T, c client.Client) {
	t.Helper()
	role := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{Name: "kube-agents-journal-test-writer"},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{"kubeagents.x-k8s.io"},
			Resources: []string{"actionrecords", "actionrecords/status"},
			Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
		}},
	}
	if err := c.Create(ctx, role); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create test ClusterRole: %v", err)
	}
	binding := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kube-agents-journal-test-writer"},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "ClusterRole", Name: role.Name},
		Subjects:   []rbacv1.Subject{{APIGroup: rbacv1.GroupName, Kind: "Group", Name: testWriterGroup}},
	}
	if err := c.Create(ctx, binding); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create test ClusterRoleBinding: %v", err)
	}
}

// applyPolicyFile creates every document in a multi-document YAML file. It is deliberately the
// SHIPPED file rather than an inline copy: a test against a hand-written equivalent proves that the
// equivalent is correct and says nothing about what provisioning installs.
func applyPolicyFile(ctx context.Context, t *testing.T, c client.Client, path string) {
	t.Helper()
	raw, err := os.ReadFile(path) // #nosec G304 -- a fixed test path
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	dec := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(raw), 4096)
	for {
		var obj unstructured.Unstructured
		if err := dec.Decode(&obj); err != nil {
			if err == io.EOF {
				return
			}
			t.Fatalf("decode %s: %v", path, err)
		}
		if len(obj.Object) == 0 {
			continue
		}
		if err := c.Create(ctx, &obj); err != nil && !apierrors.IsAlreadyExists(err) {
			t.Fatalf("apply %s %s from %s: %v", obj.GetKind(), obj.GetName(), path, err)
		}
	}
}

// waitForPolicy blocks until the policy is actually enforcing, using a write the table forbids.
// The API server loads ValidatingAdmissionPolicies asynchronously, so without this every ALLOWED
// case in the test above could pass on a cluster where nothing is enforced yet -- and the denied
// cases would fail intermittently, which is worse than failing always.
func waitForPolicy(ctx context.Context, t *testing.T, human client.Client, probe *agentv1alpha1.ActionRecord) {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		probe.Status.Message = fmt.Sprintf("policy-load probe %d", time.Now().UnixNano())
		err := human.Status().Update(ctx, probe)
		if apierrors.IsForbidden(err) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("vap-agent-scope-journal never started enforcing: a human status write was still accepted after 30s (last error: %v)", err)
		}
		time.Sleep(250 * time.Millisecond)
	}
}
