package undo

import (
	"context"
	"errors"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func at() metav1.Time { return metav1.Date(2026, 1, 1, 0, 0, 0, 0, metav1.Now().Location()) }

func createOp() Operation {
	return Operation{
		Verb: "create",
		Target: agentv1alpha1.TargetRef{
			Group: "apps", Version: "v1", Kind: "Deployment",
			Namespace: "team-x", Name: "api",
		},
	}
}

// TestStrategyTable is 06 §4.3.1 read straight down, including the row that matters most: the two
// answers `apply` gives depending on whether the object was already there.
func TestStrategyTable(t *testing.T) {
	cases := []struct {
		verb    string
		existed bool
		want    agentv1alpha1.UndoStrategy
	}{
		{"create", false, agentv1alpha1.UndoDelete},
		{"create", true, agentv1alpha1.UndoDelete},
		{"apply", true, agentv1alpha1.UndoRestore},
		{"apply", false, agentv1alpha1.UndoDelete},
		{"patch", true, agentv1alpha1.UndoRestore},
		{"scale", true, agentv1alpha1.UndoRestore},
		{"delete", true, agentv1alpha1.UndoRecreate},
		{"cloud", true, agentv1alpha1.UndoInverse},
		{"", false, agentv1alpha1.UndoNone},
		{"teleport", true, agentv1alpha1.UndoNone},
		{"DELETE", true, agentv1alpha1.UndoNone},
	}
	for _, c := range cases {
		got, err := StrategyFor(c.verb, c.existed)
		if err != nil {
			t.Errorf("StrategyFor(%q, %v): %v", c.verb, c.existed, err)
			continue
		}
		if got != c.want {
			t.Errorf("StrategyFor(%q, existed=%v) = %q, want %q", c.verb, c.existed, got, c.want)
		}
	}
}

// TestGenerateRejectsAnUnusableRequest covers the two inputs that are programmer errors rather than
// irreversible actions, and so are errors rather than refusals: there is no envelope to reason
// about, and no generation time to check the undo window against.
func TestGenerateRejectsAnUnusableRequest(t *testing.T) {
	if _, err := Generate(context.Background(), Request{GeneratedAt: at()}, fakeIndex{}); err == nil {
		t.Error("Generate accepted an envelope with no operations")
	}
	if _, err := Generate(context.Background(), Request{Operations: []Operation{createOp()}}, fakeIndex{}); err == nil {
		t.Error("Generate accepted a plan with no generation time; nothing could check it against the undo window")
	}
}

// TestBindCreatedUIDClosesTheStepSixGap is the tension in 06 §4.3.1 resolved and then pinned: the
// plan's SHAPE is decided before execution, the uid is bound after, and forgetting the second half
// fails closed.
func TestBindCreatedUIDClosesTheStepSixGap(t *testing.T) {
	res, err := Generate(context.Background(), Request{Operations: []Operation{createOp()}, GeneratedAt: at()}, fakeIndex{})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if !res.Undoable() {
		t.Fatalf("a create is undoable: %v", res.Refusals)
	}
	res.Plan.Validated = true

	// The gating decision at step 6 already has what it needs -- there is a plan, of a known shape.
	// What it does not have is the uid, because the object does not exist yet.
	if err := ValidateReplayable(res.Plan); err == nil {
		t.Fatal("an unbound delete step was accepted for replay; a delete by name alone removes whatever holds that name now")
	} else if !strings.Contains(err.Error(), "uid precondition") {
		t.Errorf("the refusal does not name the missing precondition: %v", err)
	}

	if err := BindCreatedUID(res.Plan, 0, "2f1c-real-uid"); err != nil {
		t.Fatalf("BindCreatedUID: %v", err)
	}
	if err := ValidateReplayable(res.Plan); err != nil {
		t.Errorf("a bound, validated plan was refused: %v", err)
	}

	// Every rejection here is a caller doing something that would produce a plan that damages
	// something while reporting success.
	if err := BindCreatedUID(nil, 0, "x"); err == nil {
		t.Error("bound a uid into a nil plan")
	}
	if err := BindCreatedUID(res.Plan, 7, "x"); err == nil {
		t.Error("bound a uid to a step index that does not exist")
	}
	if err := BindCreatedUID(res.Plan, 0, ""); err == nil {
		t.Error("bound an empty uid, which is what ValidateReplayable exists to catch")
	}
}

func TestBindCreatedUIDRefusesANonDeleteStep(t *testing.T) {
	res, err := Generate(context.Background(), Request{
		Operations: []Operation{{
			Verb:     "patch",
			Target:   agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "team-x", Name: "api", UID: "existing-uid"},
			Existed:  true,
			PreState: obj(map[string]any{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": map[string]any{"name": "api", "namespace": "team-x"}}),
		}},
		GeneratedAt: at(),
	}, fakeIndex{})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	// A restore's uid comes from the object that already existed and is pinned at generation time.
	if res.Plan.Steps[0].Preconditions.UID != "existing-uid" {
		t.Errorf("a restore step did not pin the uid it was generated against: %+v", res.Plan.Steps[0].Preconditions)
	}
	if err := BindCreatedUID(res.Plan, 0, "some-other-uid"); err == nil {
		t.Error("a post-execution uid was bound onto an apply step; only a create's inverse takes one")
	}
}

func TestValidateReplayableRefusesEveryUnsafePlan(t *testing.T) {
	good := agentv1alpha1.UndoStep{
		Op:            "delete",
		Target:        agentv1alpha1.TargetRef{Kind: "ConfigMap", Namespace: "team-x", Name: "c"},
		Preconditions: &agentv1alpha1.UndoPrecondition{UID: "u"},
	}
	cases := map[string]*agentv1alpha1.UndoPlan{
		"nil plan": nil,
		"strategy none": {
			Strategy: agentv1alpha1.UndoNone, Validated: true,
			Steps: []agentv1alpha1.UndoStep{good},
		},
		"no steps": {Strategy: agentv1alpha1.UndoDelete, Validated: true},
		"never dry-run": {
			Strategy: agentv1alpha1.UndoDelete,
			Steps:    []agentv1alpha1.UndoStep{good},
		},
		"apply with no body": {
			Strategy: agentv1alpha1.UndoRestore, Validated: true,
			Steps: []agentv1alpha1.UndoStep{{Op: "apply", Target: good.Target}},
		},
		"create with no body": {
			Strategy: agentv1alpha1.UndoRecreate, Validated: true,
			Steps: []agentv1alpha1.UndoStep{{Op: "create", Target: good.Target}},
		},
		"unimplemented op": {
			Strategy: agentv1alpha1.UndoRestore, Validated: true,
			Steps: []agentv1alpha1.UndoStep{{Op: "reconcile", Target: good.Target}},
		},
	}
	for name, plan := range cases {
		t.Run(name, func(t *testing.T) {
			if err := ValidateReplayable(plan); err == nil {
				t.Error("accepted for replay")
			}
		})
	}
}

// failingDryRunner is an API server that says no.
type failingDryRunner struct{ msg string }

func (f failingDryRunner) DryRun(context.Context, agentv1alpha1.UndoStep) error {
	return errors.New(f.msg)
}

type okDryRunner struct{ calls int }

func (o *okDryRunner) DryRun(context.Context, agentv1alpha1.UndoStep) error { o.calls++; return nil }

// TestValidateDowngradesRatherThanErrors is the mechanism of V-REV-003 at the validation end. A
// plan whose steps will not apply is not a plan, and the outcome has to be a gate rather than a log
// line -- an error return is a thing a caller can swallow.
func TestValidateDowngradesRatherThanErrors(t *testing.T) {
	build := func(t *testing.T) *Result {
		t.Helper()
		res, err := Generate(context.Background(), Request{Operations: []Operation{createOp()}, GeneratedAt: at()}, fakeIndex{})
		if err != nil {
			t.Fatalf("Generate: %v", err)
		}
		return res
	}

	t.Run("a failing step downgrades the plan", func(t *testing.T) {
		res := build(t)
		if err := Validate(context.Background(), res, failingDryRunner{msg: "admission webhook denied the request"}); err != nil {
			t.Fatalf("Validate returned an error instead of downgrading: %v", err)
		}
		if res.Undoable() {
			t.Fatal("a plan whose step will not apply is still reported undoable; the action would execute with a rollback that has been proven not to work")
		}
		if len(res.Plan.Steps) != 0 {
			t.Error("a refused plan still carries steps; a caller who checks Steps and not Strategy would replay them")
		}
		if res.Plan.Validated {
			t.Error("validated stayed true through a downgrade")
		}
		if !strings.Contains(strings.Join(res.Plan.Caveats, "\n"), "admission webhook denied the request") {
			t.Errorf("the API server's own message did not survive to the caveats: %v", res.Plan.Caveats)
		}
	})

	t.Run("no dry-runner is a downgrade, not a pass", func(t *testing.T) {
		res := build(t)
		if err := Validate(context.Background(), res, nil); err != nil {
			t.Fatalf("Validate: %v", err)
		}
		if res.Undoable() || res.Plan.Validated {
			t.Error("an unwired broker validated its own plan by default; `validated` would be the strongest claim in the record and nothing would have checked it")
		}
	})

	t.Run("an already-refused plan is not marked validated", func(t *testing.T) {
		res, err := Generate(context.Background(), Request{
			Operations:  []Operation{{Verb: "teleport", Target: createOp().Target}},
			GeneratedAt: at(),
		}, fakeIndex{})
		if err != nil {
			t.Fatalf("Generate: %v", err)
		}
		dr := &okDryRunner{}
		if err := Validate(context.Background(), res, dr); err != nil {
			t.Fatalf("Validate: %v", err)
		}
		if dr.calls != 0 {
			t.Errorf("dry-ran %d step(s) of a refused plan", dr.calls)
		}
		if res.Plan.Validated {
			t.Error("a refusal was marked validated, which reads to every consumer as a plan that was checked and found good")
		}
	})

	t.Run("every step is dry-run", func(t *testing.T) {
		res, err := Generate(context.Background(), Request{
			Operations: []Operation{
				createOp(),
				{Verb: "create", Target: agentv1alpha1.TargetRef{Version: "v1", Kind: "Service", Namespace: "team-x", Name: "api"}},
			},
			GeneratedAt: at(),
		}, fakeIndex{})
		if err != nil {
			t.Fatalf("Generate: %v", err)
		}
		dr := &okDryRunner{}
		if err := Validate(context.Background(), res, dr); err != nil {
			t.Fatalf("Validate: %v", err)
		}
		if dr.calls != 2 {
			t.Errorf("dry-ran %d of 2 steps", dr.calls)
		}
		if !res.Plan.Validated || !res.Undoable() {
			t.Error("a plan whose every step would apply was not marked validated")
		}
	})
}

func TestGenerateAndValidateCannotBeHalfDone(t *testing.T) {
	res, err := GenerateAndValidate(context.Background(), Request{
		Operations: []Operation{createOp()}, GeneratedAt: at(),
	}, fakeIndex{}, &okDryRunner{})
	if err != nil {
		t.Fatalf("GenerateAndValidate: %v", err)
	}
	if !res.Plan.Validated {
		t.Error("the combined call left the plan unvalidated")
	}
}

// TestCombineStrategiesLabelsAMixtureHonestly. The strategy is a label for a human; the steps are
// the plan. A mixture is not `none`, because every operation in it produced steps.
func TestCombineStrategiesLabelsAMixtureHonestly(t *testing.T) {
	cases := []struct {
		in   []agentv1alpha1.UndoStrategy
		want agentv1alpha1.UndoStrategy
	}{
		{[]agentv1alpha1.UndoStrategy{agentv1alpha1.UndoDelete}, agentv1alpha1.UndoDelete},
		{[]agentv1alpha1.UndoStrategy{agentv1alpha1.UndoRecreate}, agentv1alpha1.UndoRecreate},
		{[]agentv1alpha1.UndoStrategy{agentv1alpha1.UndoDelete, agentv1alpha1.UndoRestore}, agentv1alpha1.UndoRestore},
		{nil, agentv1alpha1.UndoNone},
	}
	for _, c := range cases {
		set := map[agentv1alpha1.UndoStrategy]bool{}
		for _, s := range c.in {
			set[s] = true
		}
		if got := combineStrategies(set); got != c.want {
			t.Errorf("combineStrategies(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSplitAPIVersion(t *testing.T) {
	cases := map[string][2]string{
		"apps/v1":                               {"apps", "v1"},
		"v1":                                    {"", "v1"},
		"storage.cnrm.cloud.google.com/v1beta1": {"storage.cnrm.cloud.google.com", "v1beta1"},
	}
	for in, want := range cases {
		g, v := splitAPIVersion(in)
		if g != want[0] || v != want[1] {
			t.Errorf("splitAPIVersion(%q) = (%q, %q), want (%q, %q)", in, g, v, want[0], want[1])
		}
	}
}
