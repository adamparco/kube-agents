package classify

import "testing"

// TestProductionLadder walks all four rungs and, more importantly, the cases where a HIGHER rung
// says "not production" and must stop the ladder. Every one of those is a case where a disjunction
// ("production if any label says so") gives the wrong answer, and a disjunction is what a reader
// implements if they see this as four places to look rather than four rungs.
func TestProductionLadder(t *testing.T) {
	cases := []struct {
		name    string
		obj     map[string]string
		ns      map[string]string
		want    bool
		wantSrc ProductionSource
	}{
		{"nothing labelled", nil, nil, false, SourceNone},

		{"rung 1: object canonical", map[string]string{LabelEnvironment: "production"}, nil, true, SourceObjectCanonical},
		{"rung 2: object alias", map[string]string{LabelEnvironmentAlias: "production"}, nil, true, SourceObjectAlias},
		{"rung 3: namespace canonical", nil, map[string]string{LabelEnvironment: "production"}, true, SourceNamespaceCanonical},
		{"rung 4: namespace alias", nil, map[string]string{LabelEnvironmentAlias: "production"}, true, SourceNamespaceAlias},

		// FIRST MATCH WINS, and a match is PRESENCE. These are the carve-out cases.
		{
			"a staging object in a production namespace is staging",
			map[string]string{LabelEnvironment: "staging"},
			map[string]string{LabelEnvironment: "production"},
			false, SourceNone,
		},
		{
			"the canonical key wins even when it disagrees with the alias",
			map[string]string{LabelEnvironment: "staging", LabelEnvironmentAlias: "production"},
			nil,
			false, SourceNone,
		},
		{
			"the object's alias beats the namespace's canonical",
			map[string]string{LabelEnvironmentAlias: "staging"},
			map[string]string{LabelEnvironment: "production"},
			false, SourceNone,
		},
		{
			"an empty-valued canonical label is PRESENT and stops the ladder",
			map[string]string{LabelEnvironment: ""},
			map[string]string{LabelEnvironment: "production"},
			false, SourceNone,
		},

		// And the direction that must still escalate.
		{
			"a production object in a staging namespace is production",
			map[string]string{LabelEnvironment: "production"},
			map[string]string{LabelEnvironment: "staging"},
			true, SourceObjectCanonical,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, src := IsProduction(tc.obj, tc.ns)
			if got != tc.want || src != tc.wantSrc {
				t.Fatalf("IsProduction = (%v, %q), want (%v, %q)", got, src, tc.want, tc.wantSrc)
			}
		})
	}
}

func TestIsProductionValue(t *testing.T) {
	accepted := []string{"production", "Production", "PRODUCTION", " production ", "\tProduction\n"}
	for _, v := range accepted {
		if !IsProductionValue(v) {
			t.Fatalf("IsProductionValue(%q) = false; the match is case-insensitive after trim", v)
		}
	}

	// `prod` IS NOT ACCEPTED. This is deliberate (see the comment on IsProductionValue) and it is
	// exactly the sort of thing a later reader "fixes", so it gets an assertion with the reason
	// attached rather than a silent absence from the accepted list.
	rejected := []string{"prod", "prd", "staging", "dev", "", "production-west", "nonprod"}
	for _, v := range rejected {
		if IsProductionValue(v) {
			t.Fatalf("IsProductionValue(%q) = true; if this is now intended, 06 §4.2 and the "+
				"NearMissProdValue lint both need updating -- `prod` is ambiguous with product/team names", v)
		}
	}
}

func TestNearMissProdValue(t *testing.T) {
	for _, v := range []string{"prod", "prd", "live", "producton"} {
		if !NearMissProdValue(v) {
			t.Fatalf("NearMissProdValue(%q) = false, want true so the webhook can warn the author", v)
		}
	}
	for _, v := range []string{"production", "staging", "dev", ""} {
		if NearMissProdValue(v) {
			t.Fatalf("NearMissProdValue(%q) = true, want false", v)
		}
	}
}
