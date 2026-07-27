package undo

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// alwaysSeen suppresses the novel-action escalation, so a case that gates does so because it could
// not be undone and not because the agent had never done it before. Without this, every case would
// pass for the wrong reason.
type alwaysSeen struct{}

func (alwaysSeen) Seen(string, string, classify.KindRef, string) bool { return true }

// TestUnplannableActionsGate is V-REV-003 end to end -- "an action with no generatable undo plan is
// reclassified gated and never auto-executes" -- and it is deliberately the whole chain rather than
// two assertions about two packages.
//
// The corpus already pins what this package refuses. What it cannot pin on its own is the thing the
// check is actually about: that a refusal CHANGES THE OUTCOME. Undoable() returning false is inert
// until something reads it, and the only consumer that matters is the classifier's step 6. So this
// test wires the real generator to the real classifier over the real fixtures, in the same order the
// broker does, and asserts on the class -- the value that decides whether the action runs.
//
// The failure it is built to catch is not a wrong verdict in either package. It is the two of them
// being individually correct and never connected: a generator that refuses beautifully into a field
// nobody feeds to the gate.
func TestUnplannableActionsGate(t *testing.T) {
	c, err := classify.New(nil, alwaysSeen{})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	cases := loadRoundTripCorpus(t)
	negatives := 0

	for _, tc := range cases {
		if tc.Expect.Undoable {
			continue
		}
		negatives++
		t.Run(tc.ID, func(t *testing.T) {
			ops, idx := tc.build(t)
			res, err := Generate(context.Background(), Request{Operations: ops, GeneratedAt: at()}, idx)
			if err != nil {
				t.Fatalf("Generate: %v", err)
			}
			if res.Undoable() {
				t.Fatalf("the corpus says this is not undoable and the generator disagrees: %v", res.Plan.Strategy)
			}

			var resolved []classify.ResolvedOp
			for _, o := range ops {
				resolved = append(resolved, classify.ResolvedOp{
					Verb:        o.Verb,
					Kind:        o.KindRef(),
					Namespace:   o.Target.Namespace,
					Name:        o.Target.Name,
					Exists:      o.Existed,
					BlastRadius: classify.BlastRadius{Objects: 1},
				})
			}

			got, err := c.Classify(&classify.Input{
				Caller: classify.Caller{
					Name:  "cluster-admin-a",
					Tier:  "cluster-admin",
					Scope: scope.Scope{ProjectID: "proj", ClusterName: "cluster-1"},
				},
				Operations: resolved,
				// THE WIRE UNDER TEST. Fed from the generator's own verdict rather than hardcoded,
				// because hardcoding it would test the classifier's step 6 -- which P9-T3a already
				// covers -- instead of the connection between the two.
				UndoPlanPresent: res.Undoable(),
			})
			if tc.Expect.ClassifierRejectsInput {
				// The action still never executes -- but by the envelope validator, not by the gate.
				// Asserted in both directions so that neither layer can quietly stop covering it.
				if err == nil {
					t.Fatalf("the corpus says the classifier rejects this envelope outright and it classified %s instead", got.Class)
				}
				return
			}
			if err != nil {
				t.Fatalf("Classify: %v", err)
			}
			if got.Class < classify.ClassGated {
				t.Fatalf("classifies %s, but no undo plan could be generated.\nAn action that cannot be rolled back and does not gate is executed with no human in the loop and no way back.\nrefusals: %v\nreasons: %v",
					got.Class, res.Refusals, got.Reasons)
			}
		})
	}

	if negatives < 10 {
		t.Fatalf("VACUOUS: only %d negative cases in the corpus; the negative set of 09 §7.3 is what this check reads and an empty loop asserts nothing", negatives)
	}
}

// TestDryRunIsExemptFromTheUndoRequirement records the one case where an unplannable action does not
// gate, so that the exemption is a decision on the record rather than a gap someone finds later.
//
// A dry run changes nothing, so there is nothing to undo. Requiring a plan for one would gate every
// preview an agent asks for, and a gate on a no-op is the fastest way to teach people that gates are
// noise.
func TestDryRunIsExemptFromTheUndoRequirement(t *testing.T) {
	c, err := classify.New(nil, alwaysSeen{})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	got, err := c.Classify(&classify.Input{
		Caller: classify.Caller{
			Name:  "developer-team-a",
			Tier:  "developer-team",
			Scope: scope.Scope{ProjectID: "proj", ClusterName: "cluster-1", Namespace: "team-x"},
		},
		Operations: []classify.ResolvedOp{{
			Verb:        "apply",
			Kind:        classify.KindRef{Group: "apps", Kind: "Deployment"},
			Namespace:   "team-x",
			Name:        "api",
			Exists:      true,
			BlastRadius: classify.BlastRadius{Objects: 1},
		}},
		UndoPlanPresent: false,
		DryRun:          true,
	})
	if err != nil {
		t.Fatalf("Classify: %v", err)
	}
	if got.Class >= classify.ClassGated {
		t.Errorf("a dry run gated for want of an undo plan: %v", got.Reasons)
	}
}

// TestGeneratedPlansSurviveTheClassifierRoundTrip is the positive direction, and it is here to stop
// the cheap way of passing the test above: a generator that refuses everything would satisfy
// V-REV-003 perfectly and be useless. Every case the corpus calls undoable must actually clear step
// 6, so the refusals are load-bearing rather than universal.
func TestGeneratedPlansSurviveTheClassifierRoundTrip(t *testing.T) {
	c, err := classify.New(nil, alwaysSeen{})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	positives := 0
	for _, tc := range loadRoundTripCorpus(t) {
		if !tc.Expect.Undoable {
			continue
		}
		positives++
		t.Run(tc.ID, func(t *testing.T) {
			ops, idx := tc.build(t)
			res, err := Generate(context.Background(), Request{Operations: ops, GeneratedAt: metav1.Now()}, idx)
			if err != nil {
				t.Fatalf("Generate: %v", err)
			}
			if !res.Undoable() {
				t.Fatalf("no plan: %v", res.Refusals)
			}
			got, err := c.Classify(&classify.Input{
				Caller: classify.Caller{
					Name:  "cluster-admin-a",
					Tier:  "cluster-admin",
					Scope: scope.Scope{ProjectID: "proj", ClusterName: "cluster-1"},
				},
				Operations: []classify.ResolvedOp{{
					Verb:        ops[0].Verb,
					Kind:        ops[0].KindRef(),
					Namespace:   ops[0].Target.Namespace,
					Name:        ops[0].Target.Name,
					Exists:      ops[0].Existed,
					BlastRadius: classify.BlastRadius{Objects: 1},
				}},
				UndoPlanPresent: true,
			})
			if err != nil {
				t.Fatalf("Classify: %v", err)
			}
			// Not asserting `routine`: plenty of these gate for reasons that have nothing to do with
			// reversibility, and they should. What must not appear is the no-undo-plan reason.
			for _, r := range got.Reasons {
				if r.Rule == classify.RuleNoUndoPlan {
					t.Errorf("a plan was generated and step 6 still reports %q: %s", r.Rule, r.Detail)
				}
			}
		})
	}

	if positives < 8 {
		t.Fatalf("VACUOUS: only %d undoable cases; a corpus of nothing but refusals would pass V-REV-003 and mean nothing", positives)
	}
}
