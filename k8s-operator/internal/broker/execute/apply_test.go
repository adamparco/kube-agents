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

package execute

import (
	"context"
	"fmt"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// recordingApplier logs every call in order, so a test can assert on the SEQUENCE rather than only
// on the outcome. The ordering rules of step 9 are the substance of this file: an executor that
// reaches the right end state by mutating before it journals is wrong in the way that matters.
type recordingApplier struct {
	calls []string

	// result is what the server says the object would become / has become, keyed by verb+dryRun.
	result func(op Op, dryRun bool) (*unstructured.Unstructured, error)

	noDryRun bool
	deleteFn func(ref agentv1alpha1.TargetRef, opts DeleteOpts, dryRun bool) error

	lastManager string
	deleteOpts  DeleteOpts
}

func (a *recordingApplier) log(f string, args ...any) {
	a.calls = append(a.calls, fmt.Sprintf(f, args...))
}

func (a *recordingApplier) Apply(_ context.Context, obj *unstructured.Unstructured, fm string, dryRun bool) (*unstructured.Unstructured, error) {
	a.log("apply(%s,dryRun=%v)", obj.GetName(), dryRun)
	a.lastManager = fm
	return a.result(Op{Verb: "apply", Desired: obj}, dryRun)
}

func (a *recordingApplier) Patch(_ context.Context, ref agentv1alpha1.TargetRef, patchType string, _ []byte, fm string, dryRun bool) (*unstructured.Unstructured, error) {
	a.log("patch(%s,%s,dryRun=%v)", ref.Name, patchType, dryRun)
	a.lastManager = fm
	return a.result(Op{Verb: "patch", Ref: ref}, dryRun)
}

func (a *recordingApplier) Scale(_ context.Context, ref agentv1alpha1.TargetRef, replicas int32, fm string, dryRun bool) (*unstructured.Unstructured, error) {
	a.log("scale(%s,%d,dryRun=%v)", ref.Name, replicas, dryRun)
	a.lastManager = fm
	return a.result(Op{Verb: "scale", Ref: ref, Replicas: &replicas}, dryRun)
}

func (a *recordingApplier) Delete(_ context.Context, ref agentv1alpha1.TargetRef, opts DeleteOpts, dryRun bool) error {
	a.log("delete(%s,dryRun=%v)", ref.Name, dryRun)
	a.deleteOpts = opts
	if a.deleteFn != nil {
		return a.deleteFn(ref, opts, dryRun)
	}
	return nil
}

func (a *recordingApplier) SupportsDryRun(context.Context, agentv1alpha1.TargetRef) bool {
	return !a.noDryRun
}

type recordingJournal struct {
	applier *recordingApplier
	err     error
}

func (j *recordingJournal) ConfirmDurable(context.Context, string) error {
	if j.applier != nil {
		j.applier.log("journal-durable")
	}
	return j.err
}

// scaledTo returns a deployment with the given replica count and a resourceVersion, standing in for
// the server's answer.
func scaledTo(n int64, rv string) *unstructured.Unstructured {
	return deployment(func(m map[string]any) {
		m["spec"].(map[string]any)["replicas"] = n
		m["metadata"].(map[string]any)["resourceVersion"] = rv
	})
}

func replicaOp(index int) Op {
	five := int32(5)
	return Op{
		Index:      index,
		Verb:       "scale",
		Ref:        agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "team-a", Name: "api"},
		Replicas:   &five,
		Classified: Classified{TargetIndex: index, Verb: "scale", TouchedPaths: []string{"/spec/replicas"}},
	}
}

func replicaSnapshot(index int) Snapshot {
	return Snapshot{
		TargetIndex: index,
		Ref:         agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "team-a", Name: "api", UID: "uid-api"},
		Existed:     true,
		Live:        deployment(nil),
		Record:      &agentv1alpha1.PreStateSnapshot{TargetIndex: int32(index), CapturedAt: metav1.Now()},
	}
}

func newExecutor(a *recordingApplier) *Executor {
	return &Executor{Applier: a, Journal: &recordingJournal{applier: a}}
}

func TestExecuteDryRunsThenJournalsThenMutates(t *testing.T) {
	// The write-ahead rule (V-REV-002): the record is durable before the first mutation. Asserted
	// on the call sequence, because the end state of a correct and an incorrect ordering is
	// identical -- the difference only shows up when the process dies in between.
	a := &recordingApplier{result: func(op Op, dryRun bool) (*unstructured.Unstructured, error) {
		if dryRun {
			return scaledTo(5, "42"), nil
		}
		return scaledTo(5, "43"), nil
	}}
	e := newExecutor(a)

	res, err := e.Execute(context.Background(), Request{
		ActionID:      "act-1",
		AgentIdentity: "cluster-admin/prod",
		Ops:           []Op{replicaOp(0)},
		Snapshots:     []Snapshot{replicaSnapshot(0)},
	})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}

	want := []string{"scale(api,5,dryRun=true)", "journal-durable", "scale(api,5,dryRun=false)"}
	if strings.Join(a.calls, " ") != strings.Join(want, " ") {
		t.Fatalf("call sequence:\n got %v\nwant %v", a.calls, want)
	}
	if res.FieldManager != "kube-agents/cluster-admin/prod" {
		t.Fatalf("field manager = %q", res.FieldManager)
	}
	if a.lastManager != res.FieldManager {
		t.Fatalf("the applier saw manager %q, the result reports %q", a.lastManager, res.FieldManager)
	}
	if !res.Outcomes[0].DryRunUsed {
		t.Fatal("DryRunUsed = false on a target whose API supports it")
	}
	if res.Outcomes[0].Applied == nil || res.Outcomes[0].Applied.ResourceVersionAfter != "43" {
		t.Fatalf("applied entry = %+v", res.Outcomes[0].Applied)
	}
}

func TestExecuteDryRunsEveryTargetBeforeMutatingAny(t *testing.T) {
	// Three targets, the third fails its integrity check. Nothing may have been mutated. The
	// natural per-target loop -- dry run, check, apply, next -- passes every single-target test and
	// fails this one, which is why it is here.
	a := &recordingApplier{result: func(op Op, dryRun bool) (*unstructured.Unstructured, error) {
		if op.Ref.Name == "bad" {
			// The server says the merge would also swap the image.
			return deployment(func(m map[string]any) {
				m["spec"].(map[string]any)["replicas"] = int64(5)
				spec := m["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)
				spec["containers"] = []any{map[string]any{"name": "api", "image": "evil:latest"}}
			}), nil
		}
		return scaledTo(5, "43"), nil
	}}
	e := newExecutor(a)

	ops := []Op{replicaOp(0), replicaOp(1), replicaOp(2)}
	ops[2].Ref.Name = "bad"
	snaps := []Snapshot{replicaSnapshot(0), replicaSnapshot(1), replicaSnapshot(2)}

	res, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "cluster-admin/prod", Ops: ops, Snapshots: snaps,
	})
	if err == nil {
		t.Fatal("an expanding change passed the executor")
	}
	if res.Mutated {
		t.Fatal("Mutated = true after a preflight failure")
	}
	for _, c := range a.calls {
		if strings.Contains(c, "dryRun=false") {
			t.Fatalf("a real mutation was issued before every target had been checked: %v", a.calls)
		}
		if c == "journal-durable" {
			t.Fatalf("the record was journalled for an action that will not run: %v", a.calls)
		}
	}
}

func TestExecuteRefusesWhenTheRecordIsNotDurable(t *testing.T) {
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "42"), nil }}
	e := &Executor{Applier: a, Journal: &recordingJournal{applier: a, err: fmt.Errorf("etcdserver: request timed out")}}

	res, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{replicaOp(0)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if err == nil {
		t.Fatal("an action executed with no durable record")
	}
	if res.Mutated {
		t.Fatal("Mutated = true after the journal refused")
	}
	for _, c := range a.calls {
		if strings.Contains(c, "dryRun=false") {
			t.Fatalf("a mutation was issued despite the journal failure: %v", a.calls)
		}
	}
}

func TestExecuteRefusesWithNoJournal(t *testing.T) {
	// Fail closed. A nil journal is a misconfiguration, and the tempting reading -- "no journal
	// configured, so nothing to wait for" -- turns the write-ahead rule off for exactly the
	// deployment that forgot to wire it.
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "42"), nil }}
	e := &Executor{Applier: a}

	if _, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{replicaOp(0)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	}); err == nil {
		t.Fatal("an executor with no journal executed")
	}
}

func TestExecuteDryRunOnlyMutatesNothing(t *testing.T) {
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "42"), nil }}
	e := newExecutor(a)

	res, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform", DryRunOnly: true,
		Ops: []Op{replicaOp(0)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Mutated {
		t.Fatal("a dry run reported Mutated")
	}
	if !res.DryRunOnly {
		t.Fatal("the result does not echo DryRunOnly; a reader could mistake it for an apply")
	}
	if res.Outcomes[0].Applied != nil {
		t.Fatal("a dry run produced an applied record entry")
	}
	if len(res.Outcomes[0].Diff.Ops) == 0 {
		t.Fatal("a dry run recorded no diff; the point of shadow mode is the diff")
	}
	if strings.Join(a.calls, " ") != "scale(api,5,dryRun=true)" {
		t.Fatalf("calls = %v", a.calls)
	}
}

func TestExecuteRequiresASnapshotPerOp(t *testing.T) {
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "42"), nil }}
	e := newExecutor(a)

	_, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{replicaOp(0), replicaOp(1)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if err == nil {
		t.Fatal("an op with no pre-state executed")
	}
	if !strings.Contains(err.Error(), "cannot be undone") {
		t.Fatalf("the error does not say why it matters: %v", err)
	}
}

func TestExecuteRefusesMisalignedClassification(t *testing.T) {
	// If the per-op classification is misaligned, the integrity check compares one op's effect
	// against another op's permission -- and passes, sometimes.
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "42"), nil }}
	e := newExecutor(a)

	op := replicaOp(0)
	op.Classified.TargetIndex = 1

	if _, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{op}, Snapshots: []Snapshot{replicaSnapshot(0)},
	}); err == nil {
		t.Fatal("a misaligned classification was accepted")
	}
}

func TestExecuteRefusesABadIdentity(t *testing.T) {
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "42"), nil }}
	e := newExecutor(a)

	if _, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "kube-agents/platform",
		Ops: []Op{replicaOp(0)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	}); err == nil {
		t.Fatal("an already-prefixed identity produced a field manager")
	}
	if len(a.calls) != 0 {
		t.Fatalf("calls were made despite the bad identity: %v", a.calls)
	}
}

func TestExecuteDegradesHonestlyWithoutDryRun(t *testing.T) {
	// "Where supported" is real. Without dry-run the check still runs, against the intended state,
	// and the outcome says so -- a control that quietly weakens is worse than one that is absent,
	// because the record reads the same either way.
	a := &recordingApplier{
		noDryRun: true,
		result:   func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "43"), nil },
	}
	e := newExecutor(a)

	res, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{replicaOp(0)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Outcomes[0].DryRunUsed {
		t.Fatal("DryRunUsed = true against an API that does not support it")
	}
	for _, c := range a.calls {
		if strings.Contains(c, "dryRun=true") {
			t.Fatalf("a dry run was issued against an API that does not support it: %v", a.calls)
		}
	}
}

func TestExecuteRefusesAPatchWithoutDryRun(t *testing.T) {
	// The broker will not model a server-side merge itself. Guessing it would produce an integrity
	// check that passes on exactly the payload the check exists to catch.
	a := &recordingApplier{
		noDryRun: true,
		result:   func(Op, bool) (*unstructured.Unstructured, error) { return scaledTo(5, "43"), nil },
	}
	e := newExecutor(a)

	op := replicaOp(0)
	op.Verb = "patch"
	op.PatchType = "application/strategic-merge-patch+json"
	op.PatchBody = []byte(`{"spec":{"replicas":5}}`)
	op.Classified.Verb = "patch"

	_, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{op}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if err == nil {
		t.Fatal("a patch against a dry-run-less API executed")
	}
	if !strings.Contains(err.Error(), "will not model a server-side merge") {
		t.Fatalf("the error does not explain the refusal: %v", err)
	}
}

func TestExecuteDeletePinsTheSnapshottedUID(t *testing.T) {
	// Deleting by name races with a recreate: the object the broker looked at and the object it
	// deletes can be two different objects with the same name.
	a := &recordingApplier{result: func(Op, bool) (*unstructured.Unstructured, error) { return nil, nil }}
	e := newExecutor(a)

	op := Op{
		Index:      0,
		Verb:       "delete",
		Ref:        agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "team-a", Name: "api"},
		Classified: Classified{TargetIndex: 0, Verb: "delete", WholeObject: true},
	}

	res, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{op}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if a.deleteOpts.UID != "uid-api" {
		t.Fatalf("delete precondition UID = %q, want the snapshotted uid", a.deleteOpts.UID)
	}
	// A delete's recorded diff is the removal of the pre-state, and it is recorded even though no
	// dry run answered for it.
	if len(res.Outcomes[0].Applied.Diff) == 0 {
		t.Fatal("a delete recorded no diff")
	}
	for _, d := range res.Outcomes[0].Applied.Diff {
		if d.Op != "remove" {
			t.Fatalf("a delete recorded %s at %s", d.Op, d.Path)
		}
	}
}

func TestExecuteReturnsPartialResultOnMidSequenceFailure(t *testing.T) {
	// The recovery ladder needs to know what landed. A nil result on error is a rollback with no
	// input.
	calls := 0
	a := &recordingApplier{result: func(op Op, dryRun bool) (*unstructured.Unstructured, error) {
		if !dryRun {
			calls++
			if calls == 2 {
				return nil, fmt.Errorf("conflict")
			}
		}
		return scaledTo(5, "43"), nil
	}}
	e := newExecutor(a)

	res, err := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops:       []Op{replicaOp(0), replicaOp(1)},
		Snapshots: []Snapshot{replicaSnapshot(0), replicaSnapshot(1)},
	})
	if err == nil {
		t.Fatal("a failed apply did not fail the execution")
	}
	if res == nil {
		t.Fatal("Execute returned no result on error; the recovery ladder has nothing to roll back")
	}
	if !res.Mutated {
		t.Fatal("Mutated = false after a mutation was issued")
	}
	if res.Outcomes[0].Applied == nil {
		t.Fatal("the successful first op has no record entry")
	}
	if res.Outcomes[1].Applied != nil {
		t.Fatal("the failed second op has a record entry")
	}
}

func TestExecuteMutatedIsSetBeforeTheFirstCall(t *testing.T) {
	// A mutating call that times out may well have landed. If Mutated were set after the call
	// returned, a timeout would leave the ladder believing there is nothing to roll back.
	a := &recordingApplier{result: func(_ Op, dryRun bool) (*unstructured.Unstructured, error) {
		if dryRun {
			return scaledTo(5, "42"), nil
		}
		return nil, fmt.Errorf("context deadline exceeded")
	}}
	e := newExecutor(a)

	res, _ := e.Execute(context.Background(), Request{
		ActionID: "act-1", AgentIdentity: "platform",
		Ops: []Op{replicaOp(0)}, Snapshots: []Snapshot{replicaSnapshot(0)},
	})
	if !res.Mutated {
		t.Fatal("Mutated = false after a mutating call that timed out")
	}
}
