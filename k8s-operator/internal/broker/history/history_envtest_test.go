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

package history

// The claims a fake cannot establish. The hermetic half in history_test.go feeds this package
// hand-built structs, and a struct can hold anything -- including a record shape a real API server
// would reject, and a `status.phase` that a real Create would silently throw away. Four claims
// therefore need a server:
//
//   - **A Verified record has to be MADE Verified in two steps.** ActionRecord carries a status
//     subresource, so client.Create drops `status` entirely: a record created with
//     `Status.Phase = Verified` comes back with an EMPTY phase, and derive correctly refuses to
//     count it. Only a subsequent Status().Update() makes it familiar. Every hermetic test here
//     sets the field on a struct and it "works"; this is the file that proves the field is real and
//     that the filter is reading the same one the server stores.
//   - **The shapes derive folds are shapes the CRD admits.** `strategy: recreate` with one
//     `steps[0].op: create` has to survive the enum and the CEL rule that ties a non-`none` strategy
//     to at least one step. If the CRD rejected it, derive would be folding a record that cannot
//     exist and every verb-class row above would be a claim about nothing.
//   - **An unknown strategy is refused by the schema**, so class's default arm is defence in depth
//     rather than a live path -- and if the enum ever gains a member, this is where it shows up.
//   - **The List is really namespaced**: a source in one namespace does not see another's records.
//     The alternative fails as a Forbidden at L2, in a cluster, months later.
//
// envtest is L1 by binding.md §Targets: a real API server, process-local, no cluster.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

var (
	testEnv *envtest.Environment
	k8s     client.Client
	scheme  = runtime.NewScheme()
)

func TestMain(m *testing.M) {
	// Not a hard exit on a missing KUBEBUILDER_ASSETS: the hermetic half must still run under a
	// plain `go test ./...`. The tests that need a server skip individually, via requireEnv.
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
		// A direct client, not a cached one -- the same choice cmd/broker makes. A cache would
		// answer the namespace-isolation test from a watch rather than from the server.
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
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the source against a real API server")
	}
}

func newNS(t *testing.T, ctx context.Context) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "hist-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create namespace: %v", err)
	}
	return ns.Name
}

// liveRecord builds a record the REAL CRD will accept -- every required field and every spec-level
// rule -- carrying one undo step. A fixture that only satisfied a fake would make this file and
// history_test.go test two different objects.
func liveRecord(ns, actionID, agent string, strategy agentv1alpha1.UndoStrategy, stepOp string) *agentv1alpha1.ActionRecord {
	sum := sha256.Sum256([]byte(actionID))
	target := agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "api-gateway"}
	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "ar-" + strings.ToLower(actionID), Namespace: ns},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            actionID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: agent, Namespace: ns},
			AgentIdentity:       "developer-team/my-project/cluster-a/" + ns,
			ActorServiceAccount: "developer-team-actor",
			Requester:           agentv1alpha1.ActionRequester{Kind: "human", ID: "slack:U02ABCDEF", Platform: "slack"},
			Trigger:             agentv1alpha1.ActionTrigger{Source: "chat", ChainID: actionID},
			Intent:              "raise api-gateway memory limit",
			IdempotencyKey:      "sha256:" + hex.EncodeToString(sum[:]),
			DryRun:              false,
			Classification: agentv1alpha1.ActionClassification{
				Class:    agentv1alpha1.RiskElevated,
				Reasons:  []agentv1alpha1.ClassificationReason{{Rule: "production-environment", Class: "+1"}},
				Undoable: true,
			},
			Targets: []agentv1alpha1.TargetRef{target},
			Undo: &agentv1alpha1.UndoPlan{
				Strategy:    strategy,
				GeneratedAt: metav1.NewTime(fixtureNow),
				Validated:   true,
				Steps:       []agentv1alpha1.UndoStep{{Op: stepOp, Target: target}},
			},
			Retention: agentv1alpha1.RetentionSpec{
				Class:               agentv1alpha1.RiskElevated,
				TTL:                 "720h",
				ExpiresAt:           metav1.NewTime(fixtureNow.Add(720 * time.Hour)),
				UndoWindow:          "1h",
				UndoWindowExpiresAt: metav1.NewTime(fixtureNow.Add(time.Hour)),
			},
		},
	}
	return ar
}

// fixtureNow is fixed so the retention clocks are about ordering, never about wall time.
var fixtureNow = time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)

// liveSource builds a source reading the real server, refreshed once.
func liveSource(t *testing.T, ctx context.Context, ns string) *Source {
	t.Helper()
	clk := &fakeClock{t: testAt}
	s, err := NewSource(SourceConfig{Journal: k8s, Namespace: ns, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(ctx); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	return s
}

// THE trap this file exists for. Create drops status, so the phase the caller set never reaches the
// server; only Status().Update() writes it. A source that read some other field, or a fixture that
// assumed Create carried the phase, would look correct here and be permanently blind in production.
func TestCreateDropsThePhaseSoNothingIsFamiliarUntilStatusIsWritten(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	ar := liveRecord(ns, "01JZQ8X9K7M4N2P6R8T0V3W5YZ", testAgent, agentv1alpha1.UndoRestore, "apply")
	ar.Status.Phase = agentv1alpha1.PhaseVerified // set by the caller, and about to be discarded
	if err := k8s.Create(ctx, ar); err != nil {
		t.Fatalf("create record: %v", err)
	}

	var got agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(ar), &got); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if got.Status.Phase != "" {
		t.Fatalf("status.phase = %q after Create; the premise of this test is that the subresource drops it", got.Status.Phase)
	}
	if s := liveSource(t, ctx, ns); s.Seen(testAgent, "patch", deployKn, ns) {
		t.Fatal("a record whose phase never reached the server must not confer familiarity: the filter would be reading a field nothing writes")
	}

	// Now write the phase the way the verifier does, and the same record becomes evidence.
	got.Status.Phase = agentv1alpha1.PhaseVerified
	if err := k8s.Status().Update(ctx, &got); err != nil {
		t.Fatalf("status update: %v", err)
	}
	if s := liveSource(t, ctx, ns); !s.Seen(testAgent, "patch", deployKn, ns) {
		t.Fatal("a genuinely Verified record must confer familiarity; if it does not, the source is blind in production and every hermetic test above still passes")
	}
}

// Every strategy the reverse table reads must be a strategy the CRD stores. If one of these were
// rejected at admission, its row in verbEvidence would be describing a record that cannot exist.
func TestEveryUndoClassRoundTripsThroughTheRealCRD(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()

	cases := []struct {
		strategy agentv1alpha1.UndoStrategy
		stepOp   string
		verb     string
	}{
		{agentv1alpha1.UndoDelete, "delete", "create"},
		{agentv1alpha1.UndoRestore, "apply", "patch"},
		{agentv1alpha1.UndoRestore, "scale", "scale"},
		{agentv1alpha1.UndoRecreate, "create", "delete"},
		{agentv1alpha1.UndoInverse, "setSize", "cloud"},
	}
	for _, tc := range cases {
		t.Run(string(tc.strategy)+"/"+tc.stepOp, func(t *testing.T) {
			ns := newNS(t, ctx)
			ar := liveRecord(ns, "01JZQ8X9K7M4N2P6R8T0V3W5YZ", testAgent, tc.strategy, tc.stepOp)
			if err := k8s.Create(ctx, ar); err != nil {
				t.Fatalf("the CRD refused a record this package folds: %v", err)
			}
			ar.Status.Phase = agentv1alpha1.PhaseVerified
			if err := k8s.Status().Update(ctx, ar); err != nil {
				t.Fatalf("status update: %v", err)
			}
			s := liveSource(t, ctx, ns)
			if !s.Seen(testAgent, tc.verb, deployKn, ns) {
				t.Errorf("a stored %s/%s record must make %q familiar", tc.strategy, tc.stepOp, tc.verb)
			}
		})
	}
}

// The enum is the reason class's default arm is unreachable in practice. Asserted rather than
// assumed, because "the schema will catch it" is the belief that stops people writing the arm.
func TestTheSchemaRefusesAStrategyThisPackageWouldIgnore(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	ar := liveRecord(ns, "01JZQ8X9K7M4N2P6R8T0V3W5YZ", testAgent, agentv1alpha1.UndoStrategy("rewind"), "apply")
	err := k8s.Create(ctx, ar)
	if err == nil {
		t.Fatal("the CRD accepted strategy 'rewind'; class() would silently ignore it and the enum is not the guard this package assumes")
	}
	if !strings.Contains(err.Error(), "strategy") {
		t.Errorf("the refusal should name the field: %v", err)
	}
}

// A `none` plan carries no steps and the CEL rule permits that. It must also teach nothing -- the
// action it describes is one 06 §4.3.1 gated for having no safe inverse.
func TestAGatedIrreversibleRecordTeachesNothing(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	ar := liveRecord(ns, "01JZQ8X9K7M4N2P6R8T0V3W5YZ", testAgent, agentv1alpha1.UndoNone, "apply")
	ar.Spec.Undo.Steps = nil
	if err := k8s.Create(ctx, ar); err != nil {
		t.Fatalf("a `none` plan with no steps must be storable (06 §4.3.1 CEL rule): %v", err)
	}
	ar.Status.Phase = agentv1alpha1.PhaseVerified
	if err := k8s.Status().Update(ctx, ar); err != nil {
		t.Fatalf("status update: %v", err)
	}
	s := liveSource(t, ctx, ns)
	for _, v := range []string{"create", "apply", "patch", "scale", "delete", "cloud"} {
		if s.Seen(testAgent, v, deployKn, ns) {
			t.Errorf("an irreversible action must not make %q familiar", v)
		}
	}
}

// The List is namespaced against a real server, not merely passed a namespace option. A source that
// listed cluster-wide would be Forbidden to the broker's Role (06 §2.2.1) -- an L2 discovery, in a
// cluster, months from now.
func TestASourceDoesNotSeeAnotherNamespacesJournal(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	mine, theirs := newNS(t, ctx), newNS(t, ctx)

	ar := liveRecord(theirs, "01JZQ8X9K7M4N2P6R8T0V3W5YZ", testAgent, agentv1alpha1.UndoRestore, "apply")
	if err := k8s.Create(ctx, ar); err != nil {
		t.Fatalf("create record: %v", err)
	}
	ar.Status.Phase = agentv1alpha1.PhaseVerified
	if err := k8s.Status().Update(ctx, ar); err != nil {
		t.Fatalf("status update: %v", err)
	}

	// Familiar to a source in that namespace...
	if s := liveSource(t, ctx, theirs); !s.Seen(testAgent, "patch", deployKn, theirs) {
		t.Fatal("the record must be visible in its own namespace, or the negative below proves nothing")
	}
	// ...and invisible to one in another, asked either way round.
	s := liveSource(t, ctx, mine)
	if s.Seen(testAgent, "patch", deployKn, theirs) {
		t.Error("a source scoped to one namespace read another's journal")
	}
	if s.Seen(testAgent, "patch", deployKn, mine) {
		t.Error("a record in another namespace conferred familiarity in this one")
	}
}

// Two agents sharing one namespace -- the platform and cluster-admin tiers both write into
// kubeagents-system -- must not inherit each other's experience.
func TestTwoAgentsInOneNamespaceDoNotShareExperience(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	ar := liveRecord(ns, "01JZQ8X9K7M4N2P6R8T0V3W5YZ", "platform-my-project", agentv1alpha1.UndoRecreate, "create")
	if err := k8s.Create(ctx, ar); err != nil {
		t.Fatalf("create record: %v", err)
	}
	ar.Status.Phase = agentv1alpha1.PhaseVerified
	if err := k8s.Status().Update(ctx, ar); err != nil {
		t.Fatalf("status update: %v", err)
	}

	s := liveSource(t, ctx, ns)
	if !s.Seen("platform-my-project", "delete", deployKn, ns) {
		t.Fatal("the agent that did it must be familiar with it")
	}
	if s.Seen(testAgent, "delete", deployKn, ns) {
		t.Error("06 §4.2 says 'for this agent'; a neighbour's delete is not this agent's experience")
	}
}
