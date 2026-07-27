package undo_test

import (
	"testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// seenAll suppresses the novel-action escalation so this test measures the floor rules and not the
// history heuristic. Without it every fixture would come back elevated for a reason unrelated to
// the property under test, and the assertion would pass for the wrong reason.
type seenAll struct{}

func (seenAll) Seen(string, string, classify.KindRef, string) bool { return true }

// TestNonRecreatableKindsAreGatedByTheClassifier is the cross-package invariant, and it is the
// whole reason this package keeps its own kind list instead of importing the classifier's.
//
// The two lists answer different questions -- "does deleting this destroy data" versus "can a
// recreate restore it" -- and their memberships legitimately differ in both directions. A Secret is
// stateful and recreatable; a ComputeAddress is neither obviously stateful nor recreatable. Forcing
// them to be one list would make one of the two questions wrong.
//
// What may NOT differ is this direction:
//
//	if this package cannot restore it, the classifier must not call it routine
//
// A kind that fails this test describes a broker that decided, in one file, that an operation is
// irreversible, and in another file that it is unremarkable -- and then executed it without asking
// anyone. That is not a disagreement between two lists; it is an ungated irreversible delete.
func TestNonRecreatableKindsAreGatedByTheClassifier(t *testing.T) {
	// No policies: New builds the code floor itself, and this test is about the floor alone. A
	// ChangePolicy could only ever make the answer stricter (P9-T3b), so the floor is the weakest
	// configuration the product ships and therefore the one the invariant has to hold in.
	c, err := classify.New(nil, seenAll{})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	kinds := undo.NonRecreatableKinds()
	if len(kinds) < 10 {
		t.Fatalf("VACUOUS: only %d non-recreatable kinds; the list moved or stopped being read, and an empty loop asserts nothing", len(kinds))
	}

	for _, k := range kinds {
		t.Run(k.Kind, func(t *testing.T) {
			in := &classify.Input{
				Caller: classify.Caller{
					Name:  "cluster-admin-a",
					Tier:  "cluster-admin",
					Scope: scope.Scope{ProjectID: "proj", ClusterName: "cluster-1"},
				},
				Operations: []classify.ResolvedOp{{
					Verb:        "delete",
					Kind:        k,
					Namespace:   "team-x",
					Name:        "target",
					Exists:      true,
					BlastRadius: classify.BlastRadius{Objects: 1},
				}},
				// The undo plan is present for every OTHER reason; this test is about whether the
				// floor gates the kind on its own merits, not about step 6's backstop. Setting this
				// false would make every case pass via `no-undo-plan` and prove nothing.
				UndoPlanPresent: true,
			}
			got, err := c.Classify(in)
			if err != nil {
				t.Fatalf("Classify: %v", err)
			}
			if got.Class == classify.ClassRoutine || got.Class == classify.ClassElevated {
				t.Fatalf(
					"deleting %s classifies %s, but the undo package cannot restore it.\n"+
						"An irreversible delete that does not gate is executed without a human seeing it.\n"+
						"Fix at the classifier's definition site (statefulKinds in floor.go), not here.\n"+
						"reasons: %v",
					describe(k), got.Class, got.Reasons)
			}
		})
	}
}

func describe(k classify.KindRef) string {
	if k.Group == "" {
		return "core/" + k.Kind
	}
	return k.Group + "/" + k.Kind
}
