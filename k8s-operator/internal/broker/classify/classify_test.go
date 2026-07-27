package classify

import (
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// --- helpers -----------------------------------------------------------------------------------

func testCaller() Caller {
	return Caller{
		Name:  "cluster-admin-a",
		Tier:  "cluster-admin",
		Scope: scope.Scope{ProjectID: "proj", ClusterName: "cluster-1"},
	}
}

// seenAll and seenNone are the two ends of the novel-action input. Most tests use seenAll, because
// the novel-action `+1` would otherwise escalate every fixture and mask the rule under test.
type seenAll struct{}

func (seenAll) Seen(string, string, KindRef, string) bool { return true }

type seenNone struct{}

func (seenNone) Seen(string, string, KindRef, string) bool { return false }

func mustClassifier(t *testing.T, policies []RuleSet, hist ActionHistory) *Classifier {
	t.Helper()
	c, err := New(policies, hist)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

// op builds a ResolvedOp with the fields that are almost always the same, so a test body shows only
// what it is actually varying.
func op(verb string, group, kind, ns, name string) ResolvedOp {
	return ResolvedOp{
		Verb:        verb,
		Kind:        KindRef{Group: group, Kind: kind},
		Namespace:   ns,
		Name:        name,
		Exists:      true,
		BlastRadius: BlastRadius{Objects: 1},
	}
}

func input(ops ...ResolvedOp) *Input {
	return &Input{Caller: testCaller(), Operations: ops, UndoPlanPresent: true}
}

func classify(t *testing.T, c *Classifier, in *Input) *Classification {
	t.Helper()
	got, err := c.Classify(in)
	if err != nil {
		t.Fatalf("Classify: %v", err)
	}
	return got
}

func wantClass(t *testing.T, got *Classification, want Class) {
	t.Helper()
	if got.Class != want {
		t.Fatalf("class = %s, want %s (reasons: %v)", got.Class, want, got.Reasons)
	}
}

func hasReason(got *Classification, rule string) bool {
	for _, r := range got.Reasons {
		if r.Rule == rule {
			return true
		}
	}
	return false
}

func wantReason(t *testing.T, got *Classification, rule string) {
	t.Helper()
	if !hasReason(got, rule) {
		t.Fatalf("expected a reason from rule %q, got %v", rule, got.Reasons)
	}
}

// --- the default and the ordinary cases ---------------------------------------------------------

func TestDefaultIsRoutine(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(op("patch", "apps", "Deployment", "team-a", "web")))
	wantClass(t, got, ClassRoutine)
	wantReason(t, got, RuleDefaultRoutine)
}

func TestSecretWriteIsElevated(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(op("apply", "", "Secret", "team-a", "db-creds")))
	wantClass(t, got, ClassElevated)
	wantReason(t, got, RuleSecretWrite)
}

func TestStatefulDeleteGates(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(op("delete", "", "PersistentVolumeClaim", "team-a", "data")))
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleDestructiveStatefulDelete)
}

// --- step 1: scope short-circuits ---------------------------------------------------------------

func TestOutOfScopeIsForbiddenAndShortCircuits(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	in := input(op("patch", "apps", "Deployment", "team-a", "web"))
	// A namespace-scoped caller reaching into a different namespace.
	in.Caller.Scope = scope.Scope{ProjectID: "proj", ClusterName: "cluster-1", Namespace: "team-b"}

	got := classify(t, c, in)
	wantClass(t, got, ClassForbidden)
	wantReason(t, got, RuleOutOfScope)

	// The short-circuit: exactly one reason. Running the rest of the table would produce reasons
	// about the merits of an action this agent has no standing to propose.
	if len(got.Reasons) != 1 {
		t.Fatalf("out-of-scope must short-circuit with a single reason, got %v", got.Reasons)
	}
}

// --- step 2: the forbidden set ------------------------------------------------------------------

func TestForbiddenSetShortCircuits(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(op("delete", "kubeagents.x-k8s.io", "ActionRecord", "kube-agents-system", "ar-1")))
	wantClass(t, got, ClassForbidden)
	wantReason(t, got, RuleForbiddenSet)
	if len(got.Reasons) != 1 {
		t.Fatalf("forbidden-set must short-circuit, got %v", got.Reasons)
	}
}

// TestForbiddenIsNotReachableByEscalation is the cap at gated, stated as the property that matters:
// no combination of `+1` inputs turns a legal action into one with no approval path.
func TestForbiddenIsNotReachableByEscalation(t *testing.T) {
	c := mustClassifier(t, nil, seenNone{}) // novel-action fires: +1
	o := op("delete", "", "PersistentVolumeClaim", "team-a", "data")
	o.NamespaceLabels = map[string]string{LabelEnvironment: "production"} // production: +1
	got := classify(t, c, input(o))

	// gated (stateful delete) + two escalations, capped.
	wantClass(t, got, ClassGated)
	if got.Class == ClassForbidden {
		t.Fatal("escalation reached forbidden; `+1` must cap at gated (06 §4.2 step 4)")
	}
}

func TestEscalateCapsAtGated(t *testing.T) {
	if got := Escalate(ClassGated); got != ClassGated {
		t.Fatalf("Escalate(gated) = %s, want gated", got)
	}
	if got := Escalate(ClassForbidden); got != ClassForbidden {
		t.Fatalf("Escalate(forbidden) = %s, want forbidden", got)
	}
	if got := Escalate(ClassRoutine); got != ClassElevated {
		t.Fatalf("Escalate(routine) = %s, want elevated", got)
	}
	if got := Escalate(ClassElevated); got != ClassGated {
		t.Fatalf("Escalate(elevated) = %s, want gated", got)
	}
}

// --- step 4: escalations ------------------------------------------------------------------------

func TestProductionEscalates(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("patch", "apps", "Deployment", "team-a", "web")
	o.NamespaceLabels = map[string]string{LabelEnvironment: "production"}
	got := classify(t, c, input(o))
	wantClass(t, got, ClassElevated) // routine +1
	wantReason(t, got, RuleProductionEnvironment)
}

func TestNovelActionEscalates(t *testing.T) {
	c := mustClassifier(t, nil, seenNone{})
	got := classify(t, c, input(op("patch", "apps", "Deployment", "team-a", "web")))
	wantClass(t, got, ClassElevated)
	wantReason(t, got, RuleNovelAction)
}

// TestTwoEscalationsAreNotPlusTwo pins the "applied once" decision. Two `+1` inputs on a routine
// action give elevated, not gated: the count of escalations is not a meaningful quantity and must
// not be visible in the result.
func TestTwoEscalationsAreNotPlusTwo(t *testing.T) {
	c := mustClassifier(t, nil, seenNone{}) // +1
	o := op("patch", "apps", "Deployment", "team-a", "web")
	o.NamespaceLabels = map[string]string{LabelEnvironment: "production"} // +1
	got := classify(t, c, input(o))
	wantClass(t, got, ClassElevated)
}

// --- step 6 and the caller's own knobs ----------------------------------------------------------

func TestNoUndoPlanGates(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	in := input(op("patch", "apps", "Deployment", "team-a", "web"))
	in.UndoPlanPresent = false
	got := classify(t, c, in)
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleNoUndoPlan)
}

// A dry run has nothing to undo, so the absence of a plan must not gate it. Classification still
// runs -- a dry run of a forbidden action is still forbidden -- but this particular input does not
// apply.
func TestDryRunWithoutUndoPlanDoesNotGate(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	in := input(op("patch", "apps", "Deployment", "team-a", "web"))
	in.UndoPlanPresent = false
	in.DryRun = true
	got := classify(t, c, in)
	wantClass(t, got, ClassRoutine)
}

func TestDryRunOfForbiddenIsStillForbidden(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	in := input(op("delete", "kubeagents.x-k8s.io", "ActionRecord", "kube-agents-system", "ar-1"))
	in.DryRun = true
	got := classify(t, c, in)
	wantClass(t, got, ClassForbidden)
}

func TestRequireApprovalRaisesOnly(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	in := input(op("patch", "apps", "Deployment", "team-a", "web"))
	in.RequireApproval = true
	wantClass(t, classify(t, c, in), ClassGated)

	// And it cannot lower: a forbidden action asking for approval is still forbidden.
	in2 := input(op("delete", "kubeagents.x-k8s.io", "ActionRecord", "kube-agents-system", "ar-1"))
	in2.RequireApproval = true
	wantClass(t, classify(t, c, in2), ClassForbidden)
}

// --- the envelope is as risky as its riskiest operation -----------------------------------------

func TestEnvelopeTakesTheMaxOverOperations(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(
		op("patch", "apps", "Deployment", "team-a", "web"),          // routine
		op("apply", "", "Secret", "team-a", "creds"),                // elevated
		op("delete", "", "PersistentVolumeClaim", "team-a", "data"), // gated
	))
	wantClass(t, got, ClassGated)
}

// --- the object override ------------------------------------------------------------------------

func TestObjectOverrideRaises(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("patch", "apps", "Deployment", "team-a", "web")
	o.ObjectClassOverride = "gated"
	got := classify(t, c, input(o))
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleObjectOverride)
}

// The asymmetry again, at the object level: an annotation saying `routine` on an action the floor
// gates does NOT lower it. Otherwise the override is a self-service exemption, writable by anyone
// who can label a Deployment.
func TestObjectOverrideCannotLower(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("delete", "", "PersistentVolumeClaim", "team-a", "data")
	o.ObjectClassOverride = "routine"
	got := classify(t, c, input(o))
	wantClass(t, got, ClassGated)
}

func TestMalformedObjectOverrideGates(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("patch", "apps", "Deployment", "team-a", "web")
	o.ObjectClassOverride = "sort-of-important"
	got := classify(t, c, input(o))
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleObjectOverride)
	// The reason must show the bad value, so the person who typed it can find it.
	for _, r := range got.Reasons {
		if r.Rule == RuleObjectOverride && !strings.Contains(r.Detail, "sort-of-important") {
			t.Fatalf("the reason must quote the malformed value, got %q", r.Detail)
		}
	}
}

// --- V-GAT-011: the blast-radius ladder ---------------------------------------------------------

func TestBlastRadiusGatesOverFifty(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("delete", "apps", "Deployment", "team-a", "")
	o.BlastRadius = BlastRadius{Objects: 51}
	got := classify(t, c, input(o))
	wantClass(t, got, ClassGated)
	wantReason(t, got, RuleBlastRadiusCap)
}

func TestBlastRadiusFiftyExactlyDoesNotGate(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("delete", "apps", "Deployment", "team-a", "")
	o.BlastRadius = BlastRadius{Objects: 50}
	got := classify(t, c, input(o))
	if hasReason(got, RuleBlastRadiusCap) {
		t.Fatal("50 objects is at the threshold, not over it; the rule fires above 50")
	}
}

func TestBlastRadiusHardCapAborts(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("delete", "apps", "Deployment", "team-a", "")
	o.BlastRadius = BlastRadius{Objects: 101}
	got := classify(t, c, input(o))
	if got.Abort == nil {
		t.Fatalf("101 objects must abort, got class %s", got.Class)
	}
	if got.Abort.Rule != RuleBlastRadiusHardCap {
		t.Fatalf("abort rule = %q, want %q", got.Abort.Rule, RuleBlastRadiusHardCap)
	}
}

func TestBlastRadiusFractionAborts(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	f := 0.6
	o := op("delete", "apps", "Deployment", "team-a", "")
	o.BlastRadius = BlastRadius{Objects: 60, FractionOfScope: &f}
	got := classify(t, c, input(o))
	if got.Abort == nil {
		t.Fatal("a fraction over 0.5 must abort")
	}
}

// An unavailable denominator must NOT read as zero. A nil fraction disarms the fraction test but
// leaves the count test intact; a 0.0 would silently satisfy `<= 0.5` during exactly the outage
// conditions when a mass deletion is most likely to be the thing going wrong.
func TestNilFractionDoesNotAbortAndIsNotZero(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("delete", "apps", "Deployment", "team-a", "")
	o.BlastRadius = BlastRadius{Objects: 10, FractionOfScope: nil, DenominatorUnavailable: "cache cold"}
	got := classify(t, c, input(o))
	if got.Abort != nil {
		t.Fatalf("a nil fraction must not abort on its own: %v", got.Abort)
	}
}

func TestDenominatorFloor(t *testing.T) {
	// 2 of 3 objects in a tiny namespace is 0.67 unfloored -- over the abort line -- and 0.1 with
	// the floor of 20. The floor is what stops a routine cleanup in a small dev namespace from
	// being refused outright.
	f := fraction(2, 3)
	if f == nil {
		t.Fatal("fraction returned nil for a valid denominator")
	}
	if *f > AbortScopeFraction {
		t.Fatalf("fraction(2, 3) = %v, which aborts; the floor of %d should make it %v",
			*f, MinDenominator, 2.0/float64(MinDenominator))
	}
	if *f != 2.0/float64(MinDenominator) {
		t.Fatalf("fraction(2, 3) = %v, want %v", *f, 2.0/float64(MinDenominator))
	}
	// Above the floor the real denominator is used.
	if f := fraction(50, 200); f == nil || *f != 0.25 {
		t.Fatalf("fraction(50, 200) = %v, want 0.25", f)
	}
}

func TestEffectiveMaxObjectsTakesTheMinimum(t *testing.T) {
	// Zero means "no opinion" and is skipped, which is why this cannot be a plain min.
	if got := EffectiveMaxObjects(0, 0, 0); got != 0 {
		t.Fatalf("no opinions = %d, want 0", got)
	}
	if got := EffectiveMaxObjects(0, 10, 0, 25); got != 10 {
		t.Fatalf("min = %d, want 10", got)
	}
	// A policy trying to RAISE the cap is accepted and simply never wins.
	if got := EffectiveMaxObjects(10, 5000); got != 10 {
		t.Fatalf("a larger cap must not win: got %d, want 10", got)
	}
}

// --- V-GAT-022: live state, not the payload -----------------------------------------------------

// The classifier reads the target's LIVE labels. A payload that asserts it is not production cannot
// talk itself down, because the payload is not an input to the production ladder at all.
func TestProductionReadsLiveStateNotPayload(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	o := op("apply", "apps", "Deployment", "team-a", "web")
	// Live says production.
	o.LiveLabels = map[string]string{LabelEnvironment: "production"}
	got := classify(t, c, input(o))
	wantClass(t, got, ClassElevated)
	wantReason(t, got, RuleProductionEnvironment)

	// There is deliberately no field on ResolvedOp for the payload's labels. If one is ever added,
	// this assertion is the place to notice: the production ladder takes exactly two maps, both of
	// them live.
	if _, src := IsProduction(nil, nil); src != SourceNone {
		t.Fatal("IsProduction with no live labels must not find production")
	}
}

// --- V-GAT-017: no model in the loop ------------------------------------------------------------

// The Input type carries no prose. This test does not assert that -- the compiler does, because
// there is no field to set. What it asserts is the observable consequence: permuting everything a
// model could influence leaves the classification byte-identical. The permutation here is over the
// operation ORDER, which is the only model-controlled input that survives into this package.
func TestClassificationIsOrderIndependent(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	ops := []ResolvedOp{
		op("patch", "apps", "Deployment", "team-a", "web"),
		op("apply", "", "Secret", "team-a", "creds"),
		op("delete", "", "PersistentVolumeClaim", "team-a", "data"),
	}
	base := classify(t, c, input(ops...))

	// Every rotation must give the same class and the same reason set.
	for i := 1; i < len(ops); i++ {
		rotated := append(append([]ResolvedOp{}, ops[i:]...), ops[:i]...)
		got := classify(t, c, input(rotated...))
		if got.Class != base.Class {
			t.Fatalf("rotation %d changed the class: %s -> %s", i, base.Class, got.Class)
		}
		if len(got.Reasons) != len(base.Reasons) {
			t.Fatalf("rotation %d changed the reason count: %v vs %v", i, base.Reasons, got.Reasons)
		}
	}
}

// TestReasonsAreStablyOrdered is the other half of the byte-identical property: the same input
// classified 100 times produces the same reason ORDER. A map range anywhere in the rule loop breaks
// this and breaks it intermittently.
func TestReasonsAreStablyOrdered(t *testing.T) {
	c := mustClassifier(t, nil, seenNone{})
	o := op("delete", "", "Secret", "team-a", "creds")
	o.NamespaceLabels = map[string]string{LabelEnvironment: "production"}
	o.ObjectClassOverride = "gated"

	first := classify(t, c, input(o))
	for i := 0; i < 100; i++ {
		got := classify(t, c, input(o))
		if len(got.Reasons) != len(first.Reasons) {
			t.Fatalf("run %d: reason count changed", i)
		}
		for j := range got.Reasons {
			if got.Reasons[j] != first.Reasons[j] {
				t.Fatalf("run %d: reason %d = %+v, want %+v (nondeterministic ordering)",
					i, j, got.Reasons[j], first.Reasons[j])
			}
		}
	}
}

// --- reason quality -----------------------------------------------------------------------------

// Every rule's reason is shown to a human deciding whether to approve. An empty one makes the
// approval prompt say nothing, so Validate rejects it and this test proves the floor satisfies it.
func TestCodeFloorIsValid(t *testing.T) {
	if err := CodeFloor().Validate(true); err != nil {
		t.Fatalf("the code floor does not satisfy its own validation: %v", err)
	}
}

func TestEveryFloorRuleIDIsAccountedFor(t *testing.T) {
	// The table half must be a subset of the declared ID list, and the ID list must have no
	// duplicates. A rule in the table with no constant means the corpus can never reference it.
	declared := map[string]bool{}
	for _, id := range AllFloorRuleIDs {
		if declared[id] {
			t.Fatalf("duplicate rule id in AllFloorRuleIDs: %q", id)
		}
		declared[id] = true
	}
	for _, r := range CodeFloor().Rules {
		if !declared[r.ID] {
			t.Fatalf("table rule %q is not in AllFloorRuleIDs, so no fixture can reference it", r.ID)
		}
	}
	if len(AllFloorRuleIDs) != 17 {
		t.Fatalf("06 §4.2 specifies seventeen code-floor rules, AllFloorRuleIDs has %d", len(AllFloorRuleIDs))
	}
}

func TestPolicySourcesAlwaysIncludeTheFloor(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	got := classify(t, c, input(op("patch", "apps", "Deployment", "team-a", "web")))
	found := false
	for _, s := range got.PolicySources {
		if s == "code-floor" {
			found = true
		}
	}
	if !found {
		t.Fatalf("policySources = %v, want it to include code-floor even when nothing matched", got.PolicySources)
	}
}

// --- input validation ---------------------------------------------------------------------------

func TestClassifyRejectsMalformedInput(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})

	cases := []struct {
		name string
		in   *Input
		want string
	}{
		{"no caller", &Input{Operations: []ResolvedOp{op("patch", "apps", "Deployment", "n", "x")}}, "authenticated caller"},
		{"no operations", &Input{Caller: testCaller()}, "no operations"},
		{"malformed caller scope", &Input{
			Caller:     Caller{Name: "a", Scope: scope.Scope{Namespace: "orphan"}},
			Operations: []ResolvedOp{op("patch", "apps", "Deployment", "n", "x")},
		}, "malformed scope"},
		{"unknown verb", &Input{
			Caller:     testCaller(),
			Operations: []ResolvedOp{op("frobnicate", "apps", "Deployment", "n", "x")},
		}, "not an envelope op"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := c.Classify(tc.in)
			if err == nil {
				t.Fatalf("expected an error containing %q", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %q, want it to contain %q", err, tc.want)
			}
		})
	}
}

func TestParseClassRejectsEmpty(t *testing.T) {
	// The empty string is a MISSING field, not routine. Defaulting it to the most permissive value
	// is the exact error ParseClass exists to make impossible.
	if _, err := ParseClass(""); err == nil {
		t.Fatal("ParseClass(\"\") must be an error, not routine")
	}
	for i, name := range classNames {
		got, err := ParseClass(name)
		if err != nil || got != Class(i) {
			t.Fatalf("ParseClass(%q) = (%v, %v)", name, got, err)
		}
	}
}

func TestClassStringIsVisibleWhenOutOfRange(t *testing.T) {
	if got := Class(7).String(); got != "class(7)" {
		t.Fatalf("Class(7).String() = %q; an out-of-enum class must render visibly, not as a valid class name", got)
	}
}

// TestClusterScopedTargetsAreNotInEveryCallersScope pins the scope escape that corpus case gat-151
// found: ScopeOfTarget used to leave the CALLER's namespace in place when the target had none, so a
// cluster-scoped object resolved to the caller's own scope, which trivially contains itself. The
// effect was that a developer-team agent scoped to one namespace passed step 1 for every
// cluster-scoped object in the cluster.
//
// Kept as a direct test as well as a fixture because the fixture asserts the CLASS and this asserts
// the predicate -- if step 1 is ever reordered, one of these two still fails for the right reason.
func TestClusterScopedTargetsAreNotInEveryCallersScope(t *testing.T) {
	nsAgent := Caller{Name: "dev-team-a", Tier: "developer-team",
		Scope: scope.Scope{ProjectID: "proj", ClusterName: "c1", Namespace: "team-a"}}
	clusterAdmin := Caller{Name: "cluster-admin-1", Tier: "cluster-admin",
		Scope: scope.Scope{ProjectID: "proj", ClusterName: "c1"}}

	// A cluster-scoped target is {project, cluster, ""} -- NOT the caller's scope.
	target := ScopeOfTarget(nsAgent, "")
	if target.Namespace != "" {
		t.Fatalf("a cluster-scoped target resolved to namespace %q; it has none", target.Namespace)
	}
	if ok, _ := scope.Contains(nsAgent.Scope, target); ok {
		t.Fatal("a namespace-scoped agent must NOT contain a cluster-scoped target")
	}
	if ok, _ := scope.Contains(clusterAdmin.Scope, target); !ok {
		t.Fatal("a cluster-admin agent must contain a cluster-scoped target in its own cluster")
	}

	c := mustClassifier(t, nil, seenAll{})
	got, err := c.Classify(&Input{
		Caller:          nsAgent,
		UndoPlanPresent: true,
		Operations: []ResolvedOp{
			op("patch", "rbac.authorization.k8s.io", "ClusterRole", "", "admin"),
		},
	})
	if err != nil {
		t.Fatalf("Classify: %v", err)
	}
	if got.Class != ClassForbidden {
		t.Fatalf("class = %s, want forbidden; reasons: %v", got.Class, got.Reasons)
	}
	if got.Reasons[0].Rule != RuleOutOfScope {
		t.Fatalf("first reason = %q, want %q", got.Reasons[0].Rule, RuleOutOfScope)
	}
}

// TestEverySecurityControlCanReachAGate is the invariant that the `security-loosen` kind list used
// to violate. The direction analysis models eight controls; if any of them can conclude `loosen` and
// still not fire a floor rule, that control has a gate on paper and none in the cluster.
func TestEverySecurityControlCanReachAGate(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	for _, ctrl := range SecurityControls {
		t.Run(string(ctrl), func(t *testing.T) {
			o := op("patch", "apps", "Deployment", "team-a", "web")
			o.Direction = DirectionLoosen
			got, err := c.Classify(&Input{
				Caller:          testCaller(),
				UndoPlanPresent: true,
				Operations:      []ResolvedOp{o},
			})
			if err != nil {
				t.Fatalf("Classify: %v", err)
			}
			if got.Class != ClassGated {
				t.Fatalf("a loosening of %s classified as %s, not gated; reasons: %v", ctrl, got.Class, got.Reasons)
			}
		})
	}
}

// TestForbiddenSetNamesTheLiveAPIGroup is the mechanization of a defect that switched off five of
// the nine forbidden-set entries at once.
//
// The set was written with the group `kubeagents.gke-labs.dev`, which this operator has never
// served -- the real one is in groupversion_info.go and has always been `kubeagents.x-k8s.io`. Every
// kube-agents entry therefore matched nothing: deleting an ActionRecord, editing a ChangePolicy,
// lifting a FleetFreeze and rewriting the ApprovalRoster all fell through step 2 of the evaluation
// order and were classified on their merits, which for a `patch` of a CR nobody has a rule about is
// `routine`. The corpus agreed, because the fixtures were written from the same wrong string.
//
// This test does not compare strings. It builds an operation from the SCHEME -- the same place the
// API server gets the group -- and asserts the classifier refuses it. A future rename that updates
// groupversion_info.go and forgets floor.go fails here rather than in production.
func TestForbiddenSetNamesTheLiveAPIGroup(t *testing.T) {
	c := mustClassifier(t, nil, seenAll{})
	group := agentv1alpha1.GroupVersion.Group

	// One representative verb per control-plane kind, drawn from the forbidden set's own reasons.
	cases := []struct{ verb, kind string }{
		{"delete", "ActionRecord"},
		{"patch", "ChangePolicy"},
		{"delete", "FleetFreeze"},
		{"create", "ApprovalRoster"},
		{"patch", "Agent"},
	}
	for _, tc := range cases {
		t.Run(tc.kind, func(t *testing.T) {
			got := classify(t, c, input(op(tc.verb, group, tc.kind, "kube-agents-system", "x")))
			if got.Class != ClassForbidden {
				t.Fatalf("%s %s.%s classified %s, not forbidden. The forbidden set names a group the "+
					"scheme does not serve, so the entry matches nothing; reasons: %v",
					tc.verb, tc.kind, group, got.Class, got.Reasons)
			}
		})
	}
}
