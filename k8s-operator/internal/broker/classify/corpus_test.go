package classify

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// The classification corpus of 09 §7.1.
//
// A corpus rather than more table tests, because the property being tested is not "this rule
// works" -- classify_test.go covers that -- it is "the WHOLE evaluation order, over a wide spread
// of realistic envelopes, produces the class a human would defend". Those are different tests. A
// per-rule test tells you the stateful-delete rule gates a PVC delete; only a corpus tells you that
// a PVC delete in production, by an agent that has never done one, against an object a team marked
// `routine`, still gates and does not somehow become forbidden.
//
// It is YAML rather than Go so the cases can be read and argued about by someone who does not read
// Go -- a security reviewer signing off on the gate matrix, for instance -- and so
// dev/tests/classifier-corpus-lint.py can check its coverage without parsing an AST.
//
// Every case is HERMETIC: the fixtures are already-resolved operations, so no cluster, no network,
// and the same answer on every run forever. See resolve.go for why that split exists.

const corpusPath = "../../../../verification/fixtures/classifier-corpus.yaml"

type corpusFile struct {
	Cases []corpusCase `json:"cases"`
}

type corpusCase struct {
	ID          string       `json:"id"`
	Description string       `json:"description"`
	Caller      corpusCaller `json:"caller"`
	Ops         []corpusOp   `json:"ops"`

	UndoPlan        *bool `json:"undoPlan,omitempty"`
	DryRun          bool  `json:"dryRun,omitempty"`
	RequireApproval bool  `json:"requireApproval,omitempty"`
	MaxObjects      int   `json:"maxObjects,omitempty"`
	// Seen drives the novel-action escalation. Defaults to TRUE (the action is familiar), because
	// otherwise every case in the corpus would carry a `+1` and the rule under test would be masked.
	Seen *bool `json:"seen,omitempty"`

	Expect corpusExpect `json:"expect"`
}

type corpusCaller struct {
	Name      string `json:"name"`
	Tier      string `json:"tier"`
	Project   string `json:"project"`
	Cluster   string `json:"cluster"`
	Namespace string `json:"namespace,omitempty"`
}

type corpusOp struct {
	Verb      string `json:"verb"`
	Group     string `json:"group,omitempty"`
	Kind      string `json:"kind"`
	Namespace string `json:"namespace,omitempty"`
	Name      string `json:"name,omitempty"`

	Direction       string            `json:"direction,omitempty"`
	Objects         int               `json:"objects,omitempty"`
	Fraction        *float64          `json:"fraction,omitempty"`
	LiveLabels      map[string]string `json:"liveLabels,omitempty"`
	NamespaceLabels map[string]string `json:"namespaceLabels,omitempty"`
	Override        string            `json:"override,omitempty"`
	LowerTierOwner  string            `json:"lowerTierOwner,omitempty"`
	SecretMaterial  []corpusHit       `json:"secretMaterial,omitempty"`
	TouchedPaths    []string          `json:"touchedPaths,omitempty"`
}

type corpusHit struct {
	Namespace string `json:"namespace"`
	Secret    string `json:"secret"`
	Key       string `json:"key"`
	Where     string `json:"where,omitempty"`
	Form      string `json:"form,omitempty"`
}

type corpusExpect struct {
	Class string `json:"class,omitempty"`
	// Rules are the rule IDs that MUST appear in the reasons. Not an exhaustive list -- a case
	// asserting `destructive-stateful-delete` does not have to also enumerate `novel-action`.
	Rules []string `json:"rules,omitempty"`
	// NotRules must NOT appear. This is where the negative controls live, and it is the half that
	// catches an over-eager rule: "a tightening change must not fire security-loosen" is only
	// testable by naming the rule that must stay quiet.
	NotRules []string `json:"notRules,omitempty"`
	Abort    bool     `json:"abort,omitempty"`
}

// seenFixed answers the novel-action question from the fixture rather than from history.
type seenFixed bool

func (s seenFixed) Seen(string, string, KindRef, string) bool { return bool(s) }

func loadCorpus(t *testing.T) corpusFile {
	t.Helper()
	b, err := os.ReadFile(filepath.Clean(corpusPath))
	if err != nil {
		t.Fatalf("reading the corpus: %v", err)
	}
	var f corpusFile
	if err := yaml.UnmarshalStrict(b, &f); err != nil {
		// Strict, so a typo'd field name in a fixture is a failure rather than a case that silently
		// tests something other than what it says. A fixture that does not do what it claims is
		// worse than a missing one: it reads as coverage.
		t.Fatalf("parsing the corpus: %v", err)
	}
	return f
}

// defaultCorpusCaller is the caller a case gets when it does not name one: a cluster-admin agent
// scoped to proj/cluster-1. Most cases are not about the caller, and repeating five lines of
// identity on every one of them would bury the field each case is actually varying.
func defaultCorpusCaller() Caller {
	return Caller{
		Name:  "cluster-admin-1",
		Tier:  "cluster-admin",
		Scope: scope.Scope{ProjectID: "proj", ClusterName: "cluster-1"},
	}
}

func (c corpusCase) toInput() *Input {
	caller := defaultCorpusCaller()
	if c.Caller.Name != "" {
		caller = Caller{
			Name: c.Caller.Name,
			Tier: c.Caller.Tier,
			Scope: scope.Scope{
				ProjectID:   c.Caller.Project,
				ClusterName: c.Caller.Cluster,
				Namespace:   c.Caller.Namespace,
			},
		}
	}
	in := &Input{
		Caller:          caller,
		DryRun:          c.DryRun,
		RequireApproval: c.RequireApproval,
		MaxObjects:      c.MaxObjects,
		UndoPlanPresent: c.UndoPlan == nil || *c.UndoPlan,
	}
	for _, o := range c.Ops {
		objects := o.Objects
		if objects == 0 {
			objects = 1
		}
		ro := ResolvedOp{
			Verb:                o.Verb,
			Kind:                KindRef{Group: o.Group, Kind: o.Kind},
			Namespace:           o.Namespace,
			Name:                o.Name,
			Exists:              true,
			Direction:           Direction(o.Direction),
			LiveLabels:          o.LiveLabels,
			NamespaceLabels:     o.NamespaceLabels,
			ObjectClassOverride: o.Override,
			LowerTierOwner:      o.LowerTierOwner,
			TouchedPaths:        o.TouchedPaths,
			BlastRadius:         BlastRadius{Objects: objects, FractionOfScope: o.Fraction},
		}
		for _, h := range o.SecretMaterial {
			ro.SecretMaterial = append(ro.SecretMaterial, SecretHit{
				Namespace: h.Namespace, Secret: h.Secret, Key: h.Key, Where: h.Where, Form: h.Form,
			})
		}
		in.Operations = append(in.Operations, ro)
	}
	return in
}

func TestClassifierCorpus(t *testing.T) {
	f := loadCorpus(t)

	if len(f.Cases) < 120 || len(f.Cases) > 200 {
		t.Fatalf("09 §7.1 specifies a 120-200 case corpus, got %d", len(f.Cases))
	}

	seenIDs := map[string]bool{}
	for _, tc := range f.Cases {
		if seenIDs[tc.ID] {
			t.Fatalf("duplicate case id %q", tc.ID)
		}
		seenIDs[tc.ID] = true

		t.Run(tc.ID, func(t *testing.T) {
			seen := true
			if tc.Seen != nil {
				seen = *tc.Seen
			}
			c := mustClassifier(t, nil, seenFixed(seen))

			got, err := c.Classify(tc.toInput())
			if err != nil {
				t.Fatalf("%s: Classify errored: %v", tc.Description, err)
			}

			if tc.Expect.Abort {
				if got.Abort == nil {
					t.Fatalf("%s: expected an abort, got class %s", tc.Description, got.Class)
				}
			} else if got.Abort != nil {
				t.Fatalf("%s: unexpected abort: %v", tc.Description, got.Abort)
			}

			if tc.Expect.Class != "" {
				want, err := ParseClass(tc.Expect.Class)
				if err != nil {
					t.Fatalf("%s: bad expected class: %v", tc.Description, err)
				}
				if got.Class != want {
					t.Fatalf("%s: class = %s, want %s\nreasons: %v", tc.Description, got.Class, want, got.Reasons)
				}
			}

			for _, want := range tc.Expect.Rules {
				if !hasReason(got, want) {
					t.Fatalf("%s: expected rule %q to fire, reasons were %v", tc.Description, want, got.Reasons)
				}
			}
			for _, notWant := range tc.Expect.NotRules {
				if hasReason(got, notWant) {
					t.Fatalf("%s: rule %q fired but must not, reasons were %v", tc.Description, notWant, got.Reasons)
				}
			}
		})
	}
}

// TestCorpusCoversEveryFloorRule is V-MET-005's Go half. The python lint checks the same property
// over the YAML so it runs in the L0 chain without a Go toolchain; this one runs it against the
// rule IDs the code actually defines, which is the half that catches a rule renamed in code and not
// in the corpus.
func TestCorpusCoversEveryFloorRule(t *testing.T) {
	f := loadCorpus(t)

	covered := map[string]bool{}
	for _, tc := range f.Cases {
		for _, r := range tc.Expect.Rules {
			covered[r] = true
		}
	}

	var missing []string
	for _, id := range AllFloorRuleIDs {
		if !covered[id] {
			missing = append(missing, id)
		}
	}
	sort.Strings(missing)
	if len(missing) > 0 {
		t.Fatalf("these code-floor rules have no corpus case asserting they fire: %s\n"+
			"a floor rule with no fixture is a rule nobody has ever seen fire",
			strings.Join(missing, ", "))
	}

	// And the reverse: a corpus naming a rule that does not exist is a fixture testing nothing.
	known := map[string]bool{}
	for _, id := range AllFloorRuleIDs {
		known[id] = true
	}
	known["caller-requested-approval"] = true
	for _, tc := range f.Cases {
		for _, r := range append(append([]string{}, tc.Expect.Rules...), tc.Expect.NotRules...) {
			if !known[r] {
				t.Fatalf("case %q names rule %q, which is not a code-floor rule id", tc.ID, r)
			}
		}
	}
}

// TestCorpusIsDeterministic runs the whole corpus twice and compares. The corpus is the artifact a
// reviewer trusts; if it is not reproducible, it is a snapshot of one afternoon rather than a
// regression test.
func TestCorpusIsDeterministic(t *testing.T) {
	f := loadCorpus(t)
	c := mustClassifier(t, nil, seenFixed(true))

	for _, tc := range f.Cases {
		in := tc.toInput()
		first, err := c.Classify(in)
		if err != nil {
			continue
		}
		for i := 0; i < 3; i++ {
			again, err := c.Classify(in)
			if err != nil {
				t.Fatalf("%s: errored on re-run %d: %v", tc.ID, i, err)
			}
			if again.Class != first.Class {
				t.Fatalf("%s: class changed between runs: %s then %s", tc.ID, first.Class, again.Class)
			}
			if len(again.Reasons) != len(first.Reasons) {
				t.Fatalf("%s: reason count changed between runs", tc.ID)
			}
			for j := range again.Reasons {
				if again.Reasons[j] != first.Reasons[j] {
					t.Fatalf("%s: reason %d differs between runs: %+v vs %+v", tc.ID, j, first.Reasons[j], again.Reasons[j])
				}
			}
		}
	}
}
