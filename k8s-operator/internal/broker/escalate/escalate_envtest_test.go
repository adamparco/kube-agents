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
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/escalate"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

// V-REV-006 at L1: "a failed rollback pages AND auto-pauses the agent ¬" (09 §6.3, 04 §5.1).
//
// The check's level list was `L2` and is now `L1, L2`. The L2 instance is not going away and is not
// weakened -- it is where the pause is observed as a real `spec.operations.paused` on a real Agent,
// which needs the C-BR reconciler and an operator image rolled by digest (P9-T7c-3c-ii-b-2). What is
// added here is the half that does not need any of that and gains nothing from waiting for it: the
// broker, on a failed rollback, records a request for both effects on the action's journal entry,
// against a real API server with the real CRD.
//
// Three reasons this is worth an envtest rather than a fake:
//
//   - A CRD prunes what it does not know. `status.escalation` is a new field; if the schema were not
//     regenerated, the API server would ACCEPT the write and silently drop the field, and every fake
//     -backed assertion would still pass because a fake has no schema. The read-back is the whole
//     point.
//   - The two halves are written by two calls (Page, then Pause) against one object. That the second
//     does not erase the first is a claim about read-modify-write against a real optimistic-
//     concurrency implementation, and a fake's Update is not one.
//   - `verify.Driver` is exercised for real, with the production `escalate.Recorder` wired in as both
//     Pager and Pauser. Wiring is where this seam breaks: an escalation recorded correctly by a
//     recorder that the driver never calls is exactly as useless as one that fails to write.
//
// The mandatory negative control (09 §6) is `TestARungNeverReachedLeavesNoEscalation`. It is the
// direction that matters: a record claiming an escalation nobody requested is how a C-BR reconciler
// pauses a healthy agent.

var (
	testEnv *envtest.Environment
	k8s     client.Client
	scheme  = runtime.NewScheme()
)

func TestMain(m *testing.M) {
	// Not os.Exit(0) on a missing KUBEBUILDER_ASSETS as a shortcut: the pure-unit half of this suite
	// lives in escalate_test.go and must still run under a plain `go test ./...`. So the environment
	// is optional and the tests that need it skip individually, via requireEnv.
	if os.Getenv("KUBEBUILDER_ASSETS") != "" {
		if err := clientgoscheme.AddToScheme(scheme); err != nil {
			panic(err)
		}
		if err := agentv1alpha1.AddToScheme(scheme); err != nil {
			panic(err)
		}
		testEnv = &envtest.Environment{
			CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "config", "crd", "bases")},
			ErrorIfCRDPathMissing: true,
			Scheme:                scheme,
		}
		cfg, err := testEnv.Start()
		if err != nil {
			panic(fmt.Sprintf("start envtest: %v", err))
		}
		k8s, err = client.New(cfg, client.Options{Scheme: scheme})
		if err != nil {
			panic(fmt.Sprintf("new client: %v", err))
		}
	}
	code := m.Run()
	if testEnv != nil {
		_ = testEnv.Stop()
	}
	os.Exit(code)
}

func requireEnv(t *testing.T) {
	t.Helper()
	if k8s == nil {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the recorder against a real API server")
	}
}

func newNS(t *testing.T, ctx context.Context) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "esc-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create namespace: %v", err)
	}
	t.Cleanup(func() { _ = k8s.Delete(context.Background(), ns) })
	return ns.Name
}

// liveRecord creates a schema-valid record through the real API server, so a CRD rule this fixture
// violates is a loud failure here rather than a surprise in the pipeline.
func liveRecord(t *testing.T, ctx context.Context, ns, actionID string) *agentv1alpha1.ActionRecord {
	t.Helper()
	ar := recordFixture(actionID, ns)
	if err := k8s.Create(ctx, ar); err != nil {
		t.Fatalf("create ActionRecord %s: %v", actionID, err)
	}
	return ar
}

// A ULID: 26 characters of Crockford base32, uppercase. Varied per test so two records in one
// namespace never collide on the derived object name.
func ulid(n int) string { return fmt.Sprintf("01JQ00000000000000000000%02d", n%100) }

// --- driving the real verify.Driver -------------------------------------------------------------

// deployProber is the smallest thing that satisfies verify.Prober: it answers Get and RestartCount
// from fields and returns ErrProbeUnsupported for the six capabilities the Deployment row of
// 04 §5.1 does not consult. Deliberately NOT a copy of the verify package's own fake -- that one is
// an internal test type, and reaching for it would have meant exporting a test fake to make a test
// convenient, which is how a production surface grows a hole.
type deployProber struct{ obj *unstructured.Unstructured }

func (p *deployProber) Get(context.Context, agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	return p.obj, nil
}
func (p *deployProber) RestartCount(context.Context, agentv1alpha1.TargetRef) (int64, error) {
	return 0, nil
}
func (p *deployProber) EndpointCount(context.Context, agentv1alpha1.TargetRef) (int, error) {
	return 0, verify.ErrProbeUnsupported
}
func (p *deployProber) ProgrammedAddress(context.Context, agentv1alpha1.TargetRef) (string, error) {
	return "", verify.ErrProbeUnsupported
}
func (p *deployProber) Connectivity(context.Context, verify.ConnectivityProbe) (bool, error) {
	return false, verify.ErrProbeUnsupported
}
func (p *deployProber) AdmissionEnforcing(context.Context, agentv1alpha1.TargetRef) (bool, error) {
	return false, verify.ErrProbeUnsupported
}
func (p *deployProber) ProviderState(context.Context, agentv1alpha1.TargetRef) (verify.ProviderStatus, error) {
	return verify.ProviderStatus{}, verify.ErrProbeUnsupported
}
func (p *deployProber) AccessReview(context.Context, verify.AccessQuery) (bool, error) {
	return false, verify.ErrProbeUnsupported
}

func deployRef(ns string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "api"}
}

func deployTarget(ns string) verify.Target {
	zero := int64(0)
	return verify.Target{Ref: deployRef(ns), BaselineRestarts: &zero}
}

func healthyDeployTarget(ns string) verify.Target { return deployTarget(ns) }
func failedDeployTarget(ns string) verify.Target  { return deployTarget(ns) }

func deployObj(ns string, status map[string]any) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata":   map[string]any{"name": "api", "namespace": ns, "generation": int64(7)},
		"spec":       map[string]any{"replicas": int64(3)},
		"status":     status,
	}}
}

// advancingClock spends the settle window without spending wall time. It is separate from the
// recorder's fixed clock on purpose: the escalation's requestedAt must be the moment rung 5 was
// reached, and a shared clock would make that assertion pass for the wrong reason.
type advancingClock struct{ t time.Time }

func (c *advancingClock) Now() time.Time { return c.t }
func (c *advancingClock) Sleep(_ context.Context, d time.Duration) error {
	c.t = c.t.Add(d)
	return nil
}

func newDriver(t *testing.T, prober verify.Prober, rec *escalate.Recorder) *verify.Driver {
	t.Helper()
	c := &advancingClock{t: fixtureNow}
	return &verify.Driver{
		Prober:       prober,
		Pager:        rec,
		Pauser:       rec,
		Now:          c.Now,
		Sleep:        c.Sleep,
		PollInterval: 10 * time.Second,
	}
}

// driverThatFailsRollback verifies a Deployment that is terminally broken -- availableReplicas 0
// with a ReplicaFailure condition, which 04 §5.1 classifies as terminal rather than pending -- so
// the ladder reaches rung 3. rbErr then decides whether it stops there or climbs to rung 5.
func driverThatFailsRollback(t *testing.T, rec *escalate.Recorder, rbErr error) *verify.Driver {
	t.Helper()
	d := newDriver(t, &deployProber{obj: deployObj("", map[string]any{
		"observedGeneration": int64(7),
		"availableReplicas":  int64(0),
		"replicas":           int64(3),
		"conditions": []any{map[string]any{
			"type": "ReplicaFailure", "status": "True",
			"reason": "FailedCreate", "message": "is invalid: spec.containers[0].image",
		}},
	})}, rec)
	d.Rollback = &fixedRollbacker{err: rbErr}
	return d
}

func driverThatConverges(t *testing.T, rec *escalate.Recorder) *verify.Driver {
	t.Helper()
	d := newDriver(t, &deployProber{obj: deployObj("", map[string]any{
		"observedGeneration": int64(7),
		"availableReplicas":  int64(3),
		"replicas":           int64(3),
		"readyReplicas":      int64(3),
		"updatedReplicas":    int64(3),
	})}, rec)
	d.Rollback = &fixedRollbacker{}
	return d
}

type fixedRollbacker struct{ err error }

func (r *fixedRollbacker) Rollback(context.Context, string, string, agentv1alpha1.UndoPlan) error {
	return r.err
}

// --- the positive: a failed rollback records both halves --------------------------------------

// TestAFailedRollbackRecordsAPageAndAPause drives the REAL verify.Driver with the REAL recorder.
// Nothing here reaches into escalate directly -- if the driver stopped calling the Pauser, or called
// it with a request that does not name the record, this test fails at the read-back rather than
// passing on a recorder that works in isolation.
func TestAFailedRollbackRecordsAPageAndAPause(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)
	id := ulid(1)
	liveRecord(t, ctx, ns, id)

	rec := &escalate.Recorder{Client: k8s, Namespace: ns, Now: func() time.Time { return fixtureNow }}
	d := driverThatFailsRollback(t, rec, errors.New("the API server rejected the restore"))

	res, err := d.Run(ctx, verify.Request{
		ActionID: id, AgentIdentity: "developer-team/proj/cluster-a/team-x",
		Targets: []verify.Target{failedDeployTarget(ns)}, UndoPlan: *recordFixture(id, ns).Spec.Undo,
	})
	if err != nil {
		t.Fatalf("Run: a page delivered and a pause recorded is not an error: %v", err)
	}
	if !res.Paged || !res.Paused {
		t.Fatalf("driver reported Paged=%v Paused=%v; 04 §5.1 requires both", res.Paged, res.Paused)
	}

	got := readEscalation(t, k8s, ns, "ar-"+strings.ToLower(id))
	if !got.PageRequested {
		t.Error("no page was requested: a failed rollback that reaches nobody is the failure rung 5 exists to prevent")
	}
	if !got.PauseRequested {
		t.Error("no pause was requested: the agent stays live after an action that could not be reversed")
	}
	if got.RequestedAt == nil || !got.RequestedAt.Time.Equal(fixtureNow) {
		t.Errorf("requestedAt = %v, want the injected clock: an escalation with no time cannot be aged out or alarmed on", got.RequestedAt)
	}
	// The reason is what a human sees in `spec.operations.pauseReason` once C-BR fans this out. A
	// reason that does not name the underlying failure sends them to the wrong incident.
	if !strings.Contains(got.Reason, "rejected the restore") {
		t.Errorf("reason does not carry the rollback error: %q", got.Reason)
	}
	// The fulfilment half is C-BR's, and C-BR does not exist yet. It must be ABSENT rather than
	// zero-valued: an empty `failure` next to a set `pausedAt` would say the pause succeeded.
	if got.PagedAt != nil || got.PausedAt != nil || got.Failure != "" {
		t.Errorf("the broker filled in the fulfilment half (%+v); it has no verb that could produce those outcomes", got)
	}
}

// TestTheSecondWriterDoesNotEraseTheFirst. Page and Pause are two round trips to one object, and in
// between them anything else with a grant on this record's status can write. The recorder re-reads
// before each update for the same reason journal.Store.SetPhase does; this asserts the property that
// re-read buys, against a real optimistic-concurrency implementation rather than a fake's Update.
//
// The interleaved write is a phase change, which is what the pipeline's own step 11 does moments
// later on this same record.
func TestTheSecondWriterDoesNotEraseTheFirst(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)
	id := ulid(2)
	ar := liveRecord(t, ctx, ns, id)
	rec := &escalate.Recorder{Client: k8s, Namespace: ns, Now: func() time.Time { return fixtureNow }}

	if err := rec.Page(ctx, verify.PageRequest{
		ActionID: id, AgentIdentity: "developer-team/team-x",
		Summary: "rollback failed", RollbackError: "restore rejected",
	}); err != nil {
		t.Fatalf("Page: %v", err)
	}

	// An out-of-band status write between the two halves, through a SEPARATE object read, which is
	// how a second writer actually arrives.
	var mid agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(ar), &mid); err != nil {
		t.Fatalf("re-read: %v", err)
	}
	mid.Status.Phase = agentv1alpha1.PhaseFailed
	mid.Status.Message = "execution errored"
	if err := k8s.Status().Update(ctx, &mid); err != nil {
		t.Fatalf("interleaved phase write: %v", err)
	}

	if err := rec.Pause(ctx, verify.PauseRequest{
		ActionID: id, AgentIdentity: "developer-team/team-x", Reason: "auto-paused after a failed rollback",
	}); err != nil {
		t.Fatalf("Pause after an interleaved write: %v", err)
	}

	var live agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(ar), &live); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if live.Status.Escalation == nil {
		t.Fatal("the escalation is gone entirely")
	}
	if !live.Status.Escalation.PageRequested {
		t.Error("the pause erased the page: a paused agent nobody was told about is a silent outage")
	}
	if !live.Status.Escalation.PauseRequested {
		t.Error("the pause was lost")
	}
	if live.Status.Phase != agentv1alpha1.PhaseFailed {
		t.Errorf("the recorder clobbered the interleaved phase write: phase = %q, want Failed", live.Status.Phase)
	}
	// First writer wins on the timestamp: rung 5 happened once.
	if live.Status.Escalation.RequestedAt == nil || !live.Status.Escalation.RequestedAt.Time.Equal(fixtureNow) {
		t.Errorf("requestedAt = %v, want the first writer's stamp", live.Status.Escalation.RequestedAt)
	}
}

// TestTheFieldSurvivesTheRealSchema. A CRD prunes what its schema does not describe, silently and
// with a 200. If `make manifests` were not re-run after `ActionEscalation` landed, every fake-backed
// assertion above would still pass and the field would not exist in any cluster. Asserted through
// an UNSTRUCTURED read so the typed round-trip cannot hide a name mismatch between the Go json tag
// and the schema property.
func TestTheFieldSurvivesTheRealSchema(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)
	id := ulid(3)
	ar := liveRecord(t, ctx, ns, id)
	rec := &escalate.Recorder{Client: k8s, Namespace: ns, Now: func() time.Time { return fixtureNow }}

	if err := rec.Pause(ctx, verify.PauseRequest{
		ActionID: id, AgentIdentity: "developer-team/team-x", Reason: "auto-paused",
	}); err != nil {
		t.Fatalf("Pause: %v", err)
	}

	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(agentv1alpha1.GroupVersion.WithKind("ActionRecord"))
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(ar), u); err != nil {
		t.Fatalf("unstructured read: %v", err)
	}
	esc, found, err := unstructured.NestedMap(u.Object, "status", "escalation")
	if err != nil || !found {
		t.Fatalf("status.escalation is absent from the served object (found=%v, err=%v) -- "+
			"the CRD schema does not describe it, so the API server pruned the write and returned 200", found, err)
	}
	for _, key := range []string{"pauseRequested", "reason", "requestedAt"} {
		if _, ok := esc[key]; !ok {
			t.Errorf("status.escalation.%s was pruned; the json tag and the schema property disagree", key)
		}
	}
}

// TestTheSchemaBoundOnReasonIsTheOneTheRecorderTruncatesTo closes the loop the pure-unit truncation
// test opens: that test proves the recorder cuts at 512, this proves 512 is what the server accepts.
// Two constants in two files agreeing is not a property; the server's acceptance is.
func TestTheSchemaBoundOnReasonIsTheOneTheRecorderTruncatesTo(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)
	id := ulid(4)
	liveRecord(t, ctx, ns, id)
	rec := &escalate.Recorder{Client: k8s, Namespace: ns, Now: func() time.Time { return fixtureNow }}

	if err := rec.Pause(ctx, verify.PauseRequest{
		ActionID: id, AgentIdentity: "developer-team/team-x",
		Reason: strings.Repeat("x", 4096),
	}); err != nil {
		t.Fatalf("a 4096-character reason was rejected rather than truncated: %v. "+
			"The brake was traded for the diagnostic", err)
	}
	got := readEscalation(t, k8s, ns, "ar-"+strings.ToLower(id))
	if !got.PauseRequested {
		t.Error("the pause did not survive truncation")
	}

	// The other direction: one rune over the bound must be refused by the SERVER, not by us. If it
	// were not, the truncation would be arbitrary rather than required, and a later refactor that
	// dropped it would break nothing here.
	var live agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "ar-" + strings.ToLower(id)}, &live); err != nil {
		t.Fatalf("read back: %v", err)
	}
	live.Status.Escalation.Reason = strings.Repeat("y", 513)
	if err := k8s.Status().Update(ctx, &live); err == nil {
		t.Error("the API server accepted a 513-character reason; the MaxLength=512 marker is not in the served schema, " +
			"so the recorder's truncation is guarding nothing")
	} else if !apierrors.IsInvalid(err) {
		t.Errorf("expected Invalid for an over-long reason, got: %v", err)
	}
}

// --- the mandatory negative control (09 §6) ----------------------------------------------------

// TestARungNeverReachedLeavesNoEscalation is the `¬` of V-REV-006.
//
// Everything above asserts that an escalation appears when rung 5 is climbed. None of it would fail
// if the recorder wrote an escalation on EVERY action, which is the more dangerous defect: once C-BR
// fans a recorded escalation out into `spec.operations.paused`, a spurious one is an outage of a
// healthy agent, arriving with an audit trail that says it was deserved.
//
// Both directions the driver can take without reaching rung 5 are covered, because they exit through
// different code paths: verification passing (rung 0, no recovery at all) and a rollback SUCCEEDING
// (rung 4, the ladder climbed but stopped one short).
func TestARungNeverReachedLeavesNoEscalation(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()

	t.Run("verification passed", func(t *testing.T) {
		ns := newNS(t, ctx)
		id := ulid(5)
		ar := liveRecord(t, ctx, ns, id)
		rec := &escalate.Recorder{Client: k8s, Namespace: ns, Now: func() time.Time { return fixtureNow }}
		d := driverThatConverges(t, rec)

		res, err := d.Run(ctx, verify.Request{
			ActionID: id, AgentIdentity: "developer-team/team-x",
			Targets: []verify.Target{healthyDeployTarget(ns)}, UndoPlan: *recordFixture(id, ns).Spec.Undo,
		})
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
		if res.Paged || res.Paused {
			t.Fatalf("a converged action reported Paged=%v Paused=%v", res.Paged, res.Paused)
		}
		assertNoEscalation(t, ctx, ns, ar)
	})

	t.Run("the rollback succeeded", func(t *testing.T) {
		ns := newNS(t, ctx)
		id := ulid(6)
		ar := liveRecord(t, ctx, ns, id)
		rec := &escalate.Recorder{Client: k8s, Namespace: ns, Now: func() time.Time { return fixtureNow }}
		d := driverThatFailsRollback(t, rec, nil) // nil error: the rollback works

		res, err := d.Run(ctx, verify.Request{
			ActionID: id, AgentIdentity: "developer-team/team-x",
			Targets: []verify.Target{failedDeployTarget(ns)}, UndoPlan: *recordFixture(id, ns).Spec.Undo,
		})
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
		if res.Decision != verify.DecisionRolledBack {
			t.Fatalf("decision = %s, want RolledBack -- this case must stop at rung 4", res.Decision)
		}
		assertNoEscalation(t, ctx, ns, ar)
	})
}

// assertNoEscalation reads the served object as unstructured: a typed read cannot distinguish a
// field that is absent from one that round-tripped as an empty struct, and "absent" is the property.
func assertNoEscalation(t *testing.T, ctx context.Context, ns string, ar *agentv1alpha1.ActionRecord) {
	t.Helper()
	u := &unstructured.Unstructured{}
	u.SetGroupVersionKind(agentv1alpha1.GroupVersion.WithKind("ActionRecord"))
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(ar), u); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if _, found, _ := unstructured.NestedMap(u.Object, "status", "escalation"); found {
		esc, _, _ := unstructured.NestedMap(u.Object, "status", "escalation")
		t.Fatalf("a rung that was never reached left status.escalation = %v behind. Once C-BR fans "+
			"this out, that is a healthy agent stopped with an audit trail saying it was deserved", esc)
	}
}
