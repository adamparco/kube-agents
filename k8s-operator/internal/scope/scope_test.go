package scope

import "testing"

func TestContains(t *testing.T) {
	p := func(proj, cl, ns string) Scope { return Scope{ProjectID: proj, ClusterName: cl, Namespace: ns} }

	cases := []struct {
		name       string
		outer      Scope
		inner      Scope
		want       bool
		wantClause Clause
	}{
		{"a scope contains itself", p("a", "b", "c"), p("a", "b", "c"), true, ClauseNone},
		{"fleet contains everything", Scope{}, p("a", "b", "c"), true, ClauseNone},
		{"project contains its clusters", p("a", "", ""), p("a", "b", ""), true, ClauseNone},
		{"project contains its namespaces", p("a", "", ""), p("a", "b", "c"), true, ClauseNone},
		{"cluster contains its namespaces", p("a", "b", ""), p("a", "b", "c"), true, ClauseNone},

		{"different project", p("a", "", ""), p("z", "b", "c"), false, ClauseProject},
		{"different cluster", p("a", "b", ""), p("a", "z", "c"), false, ClauseCluster},
		{"different namespace", p("a", "b", "c"), p("a", "b", "z"), false, ClauseNamespace},

		// The direction that is easy to get backwards: a NARROWER outer does not contain a WIDER
		// inner. A namespace-scoped agent does not contain its own cluster.
		{"namespace does not contain its cluster", p("a", "b", "c"), p("a", "b", ""), false, ClauseNamespace},
		{"cluster does not contain its project", p("a", "b", ""), p("a", "", ""), false, ClauseCluster},

		// Same namespace name in a different project must NOT be contained. This is the failure a
		// mid-scope wildcard would cause.
		{"same namespace name, different project", p("a", "b", "c"), p("z", "b", "c"), false, ClauseProject},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, clause := Contains(tc.outer, tc.inner)
			if got != tc.want || clause != tc.wantClause {
				t.Fatalf("Contains(%+v, %+v) = (%v, %v), want (%v, %v)",
					tc.outer, tc.inner, got, clause, tc.want, tc.wantClause)
			}
		})
	}
}

func TestStrictlyContains(t *testing.T) {
	p := func(proj, cl, ns string) Scope { return Scope{ProjectID: proj, ClusterName: cl, Namespace: ns} }

	// The clause that distinguishes the two predicates: equality contains but does not STRICTLY
	// contain, and reports ClauseEqual so a caller can say "you narrowed nothing" rather than "you
	// narrowed the wrong thing".
	if ok, clause := StrictlyContains(p("a", "b", "c"), p("a", "b", "c")); ok || clause != ClauseEqual {
		t.Fatalf("equal scopes: got (%v, %v), want (false, ClauseEqual)", ok, clause)
	}
	if ok, clause := StrictlyContains(p("a", "", ""), p("a", "b", "")); !ok || clause != ClauseNone {
		t.Fatalf("project over cluster: got (%v, %v), want (true, ClauseNone)", ok, clause)
	}
	// A failed containment reports its own clause, not ClauseEqual.
	if ok, clause := StrictlyContains(p("a", "", ""), p("z", "", "")); ok || clause != ClauseProject {
		t.Fatalf("different project: got (%v, %v), want (false, ClauseProject)", ok, clause)
	}
}

func TestIsWellFormed(t *testing.T) {
	cases := []struct {
		name string
		s    Scope
		want bool
	}{
		{"fleet", Scope{}, true},
		{"project", Scope{ProjectID: "a"}, true},
		{"cluster", Scope{ProjectID: "a", ClusterName: "b"}, true},
		{"namespace", Scope{ProjectID: "a", ClusterName: "b", Namespace: "c"}, true},

		// A hole in the middle would be read by Contains as a wildcard, so it must never reach it.
		{"namespace with no cluster", Scope{ProjectID: "a", Namespace: "c"}, false},
		{"namespace with nothing above", Scope{Namespace: "c"}, false},
		{"cluster with no project", Scope{ClusterName: "b"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.s.IsWellFormed(); got != tc.want {
				t.Fatalf("IsWellFormed(%+v) = %v, want %v", tc.s, got, tc.want)
			}
		})
	}
}

// TestMalformedScopeWouldWildcard is the reason IsWellFormed exists, written as an executable
// warning rather than a comment: a scope with a hole DOES contain things it must not, so the guard
// is the only thing standing between that shape and a cross-project match.
func TestMalformedScopeWouldWildcard(t *testing.T) {
	holed := Scope{ProjectID: "", ClusterName: "shared", Namespace: "team-a"}
	victim := Scope{ProjectID: "someone-elses-project", ClusterName: "shared", Namespace: "team-a"}

	if holed.IsWellFormed() {
		t.Fatal("precondition: the holed scope must be rejected by IsWellFormed")
	}
	if ok, _ := Contains(holed, victim); !ok {
		t.Fatal("precondition changed: Contains no longer treats an empty level as a wildcard; " +
			"if that is deliberate, this test and the IsWellFormed contract both need rewriting")
	}
}

func TestDepth(t *testing.T) {
	cases := []struct {
		s    Scope
		want int
	}{
		{Scope{}, 0},
		{Scope{ProjectID: "a"}, 1},
		{Scope{ProjectID: "a", ClusterName: "b"}, 2},
		{Scope{ProjectID: "a", ClusterName: "b", Namespace: "c"}, 3},
	}
	for _, tc := range cases {
		if got := tc.s.Depth(); got != tc.want {
			t.Fatalf("Depth(%+v) = %d, want %d", tc.s, got, tc.want)
		}
	}
}
