package undo

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The 09 §7.3 round-trip corpus, run as a table.
//
// The cases live in YAML rather than in this file so they can be read and argued about by someone
// who does not read Go -- a reviewer checking that the negative set really covers the effects the
// spec names -- and so a lint can check coverage without parsing an AST. Same arrangement as the
// classifier corpus, for the same reasons.
const roundTripCorpusPath = "../../../../verification/fixtures/undo/round-trip.yaml"

type corpusFile struct {
	Cases []corpusCase `json:"cases"`
}

type corpusCase struct {
	ID          string `json:"id"`
	Description string `json:"description"`

	// Operation and Operations are alternatives: the single-operation form is the common case and
	// reads better, the plural form exists for the envelope-level cases. Exactly one must be set.
	Operation  *corpusOp  `json:"operation"`
	Operations []corpusOp `json:"operations"`

	Expect corpusExpect `json:"expect"`
}

type corpusOp struct {
	Verb           string                     `json:"verb"`
	Target         agentv1alpha1.TargetRef    `json:"target"`
	Existed        bool                       `json:"existed"`
	PreState       *unstructured.Unstructured `json:"preState"`
	SnapshotFailed bool                       `json:"snapshotFailed"`
	IsStatusTarget bool                       `json:"isStatusTarget"`
	PriorReplicas  *int32                     `json:"priorReplicas"`

	InboundRefs []corpusRef `json:"inboundRefs"`
	// NoReferenceIndex runs the case with a nil index, which is the unwired-broker path. Spelled as
	// its own field rather than inferred from an absent inboundRefs list, because "no refs" and "no
	// index" are precisely the two answers this package must not conflate -- a corpus that expressed
	// them the same way could not test the distinction it exists to protect.
	NoReferenceIndex bool `json:"noReferenceIndex"`
	// ReferenceIndexError makes the index fail.
	ReferenceIndexError string `json:"referenceIndexError"`
}

type corpusRef struct {
	Kind      string `json:"kind"`
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Via       string `json:"via"`
}

type corpusExpect struct {
	Strategy        string   `json:"strategy"`
	Undoable        bool     `json:"undoable"`
	StepOps         []string `json:"stepOps"`
	RefusalContains string   `json:"refusalContains"`
	NoSteps         bool     `json:"noSteps"`
	RedactedKeys    []string `json:"redactedKeys"`

	// ClassifierRejectsInput marks a case the classifier refuses to classify at all, because the
	// envelope itself is malformed. Such an action never executes either, so V-REV-003's guarantee
	// holds -- but it holds by a different mechanism than the gate, and the gating test asserts the
	// rejection rather than accepting any error.
	ClassifierRejectsInput bool `json:"classifierRejectsInput"`
}

// fakeIndex answers from the fixture.
type fakeIndex struct {
	refs []InboundRef
	err  error
}

func (f fakeIndex) InboundReferences(context.Context, agentv1alpha1.TargetRef) ([]InboundRef, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.refs, nil
}

func loadRoundTripCorpus(t *testing.T) []corpusCase {
	t.Helper()
	b, err := os.ReadFile(roundTripCorpusPath)
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	var f corpusFile
	if err := yaml.Unmarshal(b, &f); err != nil {
		t.Fatalf("parse corpus: %v", err)
	}
	if len(f.Cases) == 0 {
		t.Fatal("VACUOUS: the corpus parsed to zero cases; a renamed key would produce exactly this and every assertion below would pass")
	}
	return f.Cases
}

// TestRoundTripCorpus is V-REV-004 (per-verb round-trip) and V-REV-003 (no plan => gated), read off
// the same fixtures a human reviews.
func TestRoundTripCorpus(t *testing.T) {
	cases := loadRoundTripCorpus(t)
	seen := map[string]bool{}

	for _, tc := range cases {
		if tc.ID == "" {
			t.Fatal("a case has no id; the ids are how a failure names itself and how the lint tracks coverage")
		}
		if seen[tc.ID] {
			t.Fatalf("duplicate case id %q: the second one silently replaces the first in every report", tc.ID)
		}
		seen[tc.ID] = true

		t.Run(tc.ID, func(t *testing.T) {
			ops, idx := tc.build(t)
			res, err := Generate(context.Background(), Request{
				Operations:  ops,
				GeneratedAt: metav1.Date(2026, 1, 1, 0, 0, 0, 0, metav1.Now().Location()),
			}, idx)
			if err != nil {
				t.Fatalf("Generate returned an error, but every outcome in this corpus is a plan or a refusal: %v", err)
			}
			tc.check(t, res)
		})
	}
}

func (tc corpusCase) build(t *testing.T) ([]Operation, ReferenceIndex) {
	t.Helper()
	raw := tc.Operations
	if tc.Operation != nil {
		if len(raw) > 0 {
			t.Fatalf("case %q sets both `operation` and `operations`", tc.ID)
		}
		raw = []corpusOp{*tc.Operation}
	}
	if len(raw) == 0 {
		t.Fatalf("case %q has no operations", tc.ID)
	}

	// One index for the whole envelope, since the broker has one. Where several operations in a case
	// carry reference expectations the last one wins, which no case in the corpus relies on -- and if
	// one ever does, it will fail loudly rather than read the wrong refs.
	var idx ReferenceIndex = fakeIndex{}
	ops := make([]Operation, 0, len(raw))
	for _, o := range raw {
		if o.NoReferenceIndex {
			idx = nil
		} else if o.ReferenceIndexError != "" {
			idx = fakeIndex{err: fmt.Errorf("%s", o.ReferenceIndexError)}
		} else if len(o.InboundRefs) > 0 {
			var refs []InboundRef
			for _, r := range o.InboundRefs {
				refs = append(refs, InboundRef{
					Ref: agentv1alpha1.TargetRef{
						Kind:      r.Kind,
						Namespace: r.Namespace,
						Name:      r.Name,
					},
					Via: r.Via,
				})
			}
			idx = fakeIndex{refs: refs}
		}
		ops = append(ops, Operation{
			Verb:           o.Verb,
			Target:         o.Target,
			Existed:        o.Existed,
			PreState:       o.PreState,
			SnapshotFailed: o.SnapshotFailed,
			IsStatusTarget: o.IsStatusTarget,
			PriorReplicas:  o.PriorReplicas,
		})
	}
	return ops, idx
}

func (tc corpusCase) check(t *testing.T, res *Result) {
	t.Helper()

	if got := string(res.Plan.Strategy); got != tc.Expect.Strategy {
		t.Errorf("strategy = %q, want %q\nrefusals: %v\ncaveats: %v", got, tc.Expect.Strategy, res.Refusals, res.Plan.Caveats)
	}

	// The bool the broker feeds to the classifier. Asserted separately from the strategy because it
	// is the value that decides whether the action gates, and a refactor that changed one without the
	// other would be caught here rather than in production.
	if got := res.Undoable(); got != tc.Expect.Undoable {
		t.Errorf("Undoable() = %v, want %v", got, tc.Expect.Undoable)
	}

	if tc.Expect.RefusalContains != "" {
		joined := strings.Join(res.Refusals, "\n")
		if !strings.Contains(joined, tc.Expect.RefusalContains) {
			t.Errorf("no refusal contains %q\ngot:\n%s", tc.Expect.RefusalContains, joined)
		}
		// A refusal that never reaches the plan never reaches a human, since the plan is what the
		// ActionRecord carries.
		if !strings.Contains(strings.Join(res.Plan.Caveats, "\n"), tc.Expect.RefusalContains) {
			t.Errorf("the refusal is in Result.Refusals but not in Plan.Caveats, so the recorded plan does not say why it refused")
		}
	}

	if !tc.Expect.Undoable {
		// A refused plan carries no steps -- see Generate. Checked on every negative case rather than
		// only where the fixture asks, because this is the property that stops a caller who reads
		// Steps without reading Strategy.
		if len(res.Plan.Steps) != 0 {
			t.Errorf("a refused plan carries %d step(s); it must carry none", len(res.Plan.Steps))
		}
		if len(res.Refusals) == 0 {
			t.Error("not undoable, but no refusal says why")
		}
		if err := ValidateReplayable(res.Plan); err == nil {
			t.Error("ValidateReplayable accepted a refused plan")
		}
		return
	}

	var gotOps []string
	for _, s := range res.Plan.Steps {
		gotOps = append(gotOps, s.Op)
	}
	if strings.Join(gotOps, ",") != strings.Join(tc.Expect.StepOps, ",") {
		t.Errorf("step ops = %v, want %v", gotOps, tc.Expect.StepOps)
	}

	for i, s := range res.Plan.Steps {
		switch s.Op {
		case "apply", "create", "scale":
			if s.Object == nil {
				t.Errorf("step %d (%s) carries no object", i, s.Op)
			}
		case "delete":
			// Unbound on purpose at generation time; BindCreatedUID fills it at step 9.
			if s.Preconditions == nil {
				t.Errorf("step %d (delete) has nil preconditions, so BindCreatedUID has nowhere to write", i)
			}
		}
	}

	// Every plan this corpus calls undoable must survive replay validation once it is bound and
	// validated. Otherwise "undoable" means "generated something", which is the weaker claim.
	bindAndValidate(t, res.Plan)
	if err := ValidateReplayable(res.Plan); err != nil {
		t.Errorf("a plan the corpus calls undoable is not replayable: %v", err)
	}

	if len(tc.Expect.RedactedKeys) > 0 {
		var got []string
		for _, reds := range res.Redactions {
			for _, r := range reds {
				got = append(got, r.Key)
			}
		}
		if strings.Join(got, ",") != strings.Join(tc.Expect.RedactedKeys, ",") {
			t.Errorf("redacted keys = %v, want %v", got, tc.Expect.RedactedKeys)
		}
		assertNoSecretValuesInPlan(t, res.Plan)
	}
}

// bindAndValidate does what the broker does between step 6 and step 9, so the corpus can assert
// replayability without a cluster.
func bindAndValidate(t *testing.T, plan *agentv1alpha1.UndoPlan) {
	t.Helper()
	for i, s := range plan.Steps {
		if s.Op == "delete" {
			if err := BindCreatedUID(plan, i, fmt.Sprintf("bound-uid-%d", i)); err != nil {
				t.Fatalf("BindCreatedUID(%d): %v", i, err)
			}
		}
	}
	plan.Validated = true
}

// assertNoSecretValuesInPlan is the property 06 §4.3.1 states as an absolute: the plan carries
// digests, and the material lives in the journal store. Asserted against the serialized bytes rather
// than the struct, because the leak this prevents is a value surviving somewhere in the object graph
// that a field-by-field check would not think to look.
func assertNoSecretValuesInPlan(t *testing.T, plan *agentv1alpha1.UndoPlan) {
	t.Helper()
	b, err := yaml.Marshal(plan)
	if err != nil {
		t.Fatalf("marshal plan: %v", err)
	}
	// The literal from the fixture, base64 and plain. A digest of it is fine; the value is not.
	for _, forbidden := range []string{"super-secret-value-here", "c3VwZXItc2VjcmV0LXZhbHVlLWhlcmU="} {
		if strings.Contains(string(b), forbidden) {
			t.Errorf("the serialized undo plan contains a Secret value (%q); only digests may leave this package", forbidden)
		}
	}
}
