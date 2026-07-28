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

package agentlabels

import (
	"fmt"
	"regexp"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// V-RUN-011 is a PROPERTY check, not an example check: "property-test the label renderer over
// scopes colliding in the first 63 chars, differing only in case, or containing invalid
// characters". So the suite is built in three layers -- an adversarial corpus, a generated sweep,
// and a negative control proving the corpus is actually adversarial. The negative control is the
// one that matters: a corpus that a NAIVE truncating renderer also survives proves nothing about
// the real one, and that is the shape in which this kind of test dies.

// collisionCorpus is the hand-built adversarial set. Every entry is a real shape: GCP project ids
// run to 30 characters, GKE cluster names to 40, namespaces to 63, and teams name namespaces with
// long shared prefixes precisely because the suffix is the part they care about.
var collisionCorpus = []scope.Scope{
	// 1-2. Differ only after byte 63 -- the classic truncation collision.
	{ProjectID: "acme-prod-platform-engineering", ClusterName: "us-east4-primary-workloads-cluster", Namespace: "payments-api-frontend"},
	{ProjectID: "acme-prod-platform-engineering", ClusterName: "us-east4-primary-workloads-cluster", Namespace: "payments-api-backend"},
	// 3-4. Differ only in case. A renderer that lowercases without hashing merges two namespaces
	// that Kubernetes considers distinct objects only at the CR level -- but two AGENTS, two actor
	// SAs, and two blast radii either way.
	{ProjectID: "acme-prod", ClusterName: "eu-west1", Namespace: "Payments"},
	{ProjectID: "acme-prod", ClusterName: "eu-west1", Namespace: "payments"},
	// 5-6. Differ only in a character no label may hold, which a sanitizer maps to the same '-'.
	{ProjectID: "acme-prod", ClusterName: "eu-west1", Namespace: "team:payments"},
	{ProjectID: "acme-prod", ClusterName: "eu-west1", Namespace: "team/payments"},
	// 7-8. Level-boundary ambiguity: the readable join merges these two, so the renderer must not
	// be allowed to pass either of them through. This pair is why the digest is taken over a
	// length-prefixed encoding rather than over the join.
	{ProjectID: "acme", ClusterName: "prod.eu", Namespace: "payments"},
	{ProjectID: "acme", ClusterName: "prod", Namespace: "eu.payments"},
	// 9-10. Depth differences -- a project-scoped platform agent and the cluster-admin beneath it.
	// Merging these merges two TIERS, and the shorter is a legal pass-through while the longer is
	// not, so they also exercise the boundary between the two sets.
	{ProjectID: "acme-prod"},
	{ProjectID: "acme-prod", ClusterName: "us-east4-primary"},
	// 11-12. Long and identical up to the very last byte.
	{ProjectID: strings.Repeat("a", 30), ClusterName: strings.Repeat("b", 40), Namespace: strings.Repeat("c", 62) + "1"},
	{ProjectID: strings.Repeat("a", 30), ClusterName: strings.Repeat("b", 40), Namespace: strings.Repeat("c", 62) + "2"},
	// 13-14. Inputs that sanitize to nothing but are still two different scopes.
	{ProjectID: "!!!", ClusterName: "@@@", Namespace: "###"},
	{ProjectID: "???", ClusterName: "***", Namespace: "%%%"},
	// 15-16. Already-hashed-LOOKING literals, which is what rule 3 exists for.
	{ProjectID: "a-0123456789"},
	{ProjectID: "a", ClusterName: "0123456789"},
}

func TestRenderScopeIsInjectiveOverTheCollisionCorpus(t *testing.T) {
	seen := map[string]scope.Scope{}
	for _, s := range collisionCorpus {
		got := RenderScope(s)
		if prior, dup := seen[got]; dup {
			t.Errorf("collision: %#v and %#v both render to %q -- 08 §2.5 keys the pod↔SA pinning "+
				"selector on this value, so two agents sharing it share a credential boundary", prior, s, got)
			continue
		}
		seen[got] = s
	}
	if len(seen) != len(collisionCorpus) {
		t.Fatalf("rendered %d distinct labels from %d distinct scopes", len(seen), len(collisionCorpus))
	}
}

// The control for the test above. A naive `truncate(lowercase(sanitize(x)), 63)` is the renderer
// anyone would write first, and it is what the repo would have if V-RUN-011 did not exist. If the
// corpus does not break it, the corpus is decoration.
func TestTheCollisionCorpusBreaksANaiveRenderer(t *testing.T) {
	naive := func(s scope.Scope) string {
		out := Sanitize(ScopeKey(s))
		if len(out) > 63 {
			out = out[:63]
		}
		return out
	}
	seen := map[string]bool{}
	collisions := 0
	for _, s := range collisionCorpus {
		v := naive(s)
		if seen[v] {
			collisions++
		}
		seen[v] = true
	}
	if collisions == 0 {
		t.Fatal("the corpus does not collide under a naive truncating renderer, so it cannot " +
			"demonstrate that the real renderer avoids collisions -- add adversarial cases")
	}
	t.Logf("naive renderer collides on %d of %d corpus scopes", collisions, len(collisionCorpus))
}

// The generated sweep. Enumerating a structured space rather than sampling one, because the
// interesting inputs here are not random -- they are near-misses.
func TestRenderScopeIsInjectiveOverAGeneratedSweep(t *testing.T) {
	var scopes []scope.Scope
	base := strings.Repeat("n", 55)
	for i := 0; i < 40; i++ {
		// Vary only past the truncation point.
		scopes = append(scopes, scope.Scope{ProjectID: "proj", ClusterName: "cluster-name-that-is-long", Namespace: fmt.Sprintf("%s%02d", base, i)})
		// Vary only in case.
		scopes = append(scopes, scope.Scope{ProjectID: "proj", ClusterName: "c", Namespace: fmt.Sprintf("Ns%02d", i)})
		scopes = append(scopes, scope.Scope{ProjectID: "proj", ClusterName: "c", Namespace: fmt.Sprintf("ns%02d", i)})
		// Vary only in an illegal character.
		scopes = append(scopes, scope.Scope{ProjectID: "proj", ClusterName: "c", Namespace: fmt.Sprintf("a:b%02d", i)})
		scopes = append(scopes, scope.Scope{ProjectID: "proj", ClusterName: "c", Namespace: fmt.Sprintf("a/b%02d", i)})
	}
	seen := map[string]scope.Scope{}
	for _, s := range scopes {
		got := RenderScope(s)
		if prior, dup := seen[got]; dup {
			t.Fatalf("collision on the generated sweep: %#v and %#v -> %q", prior, s, got)
		}
		seen[got] = s
	}
	t.Logf("%d generated scopes rendered to %d distinct labels", len(scopes), len(seen))
}

// Injectivity is worthless if the output is not a legal label value: an illegal one is not a
// collision, it is an object the API server refuses to create, and the pair never launches.
func TestRenderScopeAlwaysProducesALegalLabelValue(t *testing.T) {
	var all []scope.Scope
	all = append(all, collisionCorpus...)
	all = append(all,
		scope.Scope{},
		scope.Scope{ProjectID: "-leading-dash"},
		scope.Scope{ProjectID: "trailing-dash-"},
		scope.Scope{ProjectID: "_underscore_"},
		scope.Scope{ProjectID: strings.Repeat("x", 300)},
		scope.Scope{ProjectID: "üñïçødé"},
		scope.Scope{ProjectID: " spaces in here "},
	)
	for _, s := range all {
		got := RenderScope(s)
		if got == "" {
			// The empty scope is the only input allowed to render empty: it means "not narrowed",
			// and an empty label value is legal.
			if !s.IsZero() {
				t.Errorf("%#v rendered to the empty string", s)
			}
			continue
		}
		if errs := validation.IsValidLabelValue(got); len(errs) > 0 {
			t.Errorf("%#v -> %q is not a legal label value: %v", s, got, errs)
		}
		if len(got) > 63 {
			t.Errorf("%#v -> %q is %d bytes", s, got, len(got))
		}
	}
}

func TestRenderScopeIsDeterministic(t *testing.T) {
	for _, s := range collisionCorpus {
		first := RenderScope(s)
		for i := 0; i < 5; i++ {
			if again := RenderScope(s); again != first {
				t.Fatalf("%#v rendered %q then %q -- a renderer that is not stable rewrites the "+
					"selector on every reconcile and orphans the pods it was selecting", s, first, again)
			}
		}
	}
}

// The readable common case must stay readable, or operators stop using the label and start reading
// spec.scope out of the CR, which is what the label exists to avoid.
func TestShortLegalScopesPassThroughUnchanged(t *testing.T) {
	for _, s := range []scope.Scope{
		{ProjectID: "acme-prod"},
		{ProjectID: "acme-prod", ClusterName: "us-east4-a"},
		{ProjectID: "acme-prod", ClusterName: "us-east4-a", Namespace: "payments"},
	} {
		want := ScopeKey(s)
		if got := RenderScope(s); got != want {
			t.Errorf("%#v rendered %q, want the scope key %q unchanged", s, got, want)
		}
	}
}

// Rule 3, tested directly: the pass-through set and the hashed set must be disjoint, because that
// disjointness is the whole reason injectivity is an argument rather than a hope.
func TestAPassThroughValueNeverLooksHashed(t *testing.T) {
	literal := scope.Scope{ProjectID: "a-0123456789"}
	got := RenderScope(literal)
	if got == "a-0123456789" {
		t.Fatal("a literal scope that already looks hash-suffixed must be pushed into the hashed " +
			"set, or it can collide with the rendering of some long scope")
	}
	if !looksHashed(got) {
		t.Errorf("%q should have been rendered into the hashed set", got)
	}

	for _, s := range []string{
		"", "abc", "a-012345678", "a-012345678g", "a0123456789", "-0123456789",
	} {
		if s == "-0123456789" {
			// This one DOES look hashed: '-' then ten hex. Kept in the list as a reminder that the
			// predicate is about shape, not about plausibility.
			if !looksHashed(s) {
				t.Errorf("looksHashed(%q) = false, want true", s)
			}
			continue
		}
		if looksHashed(s) {
			t.Errorf("looksHashed(%q) = true, want false", s)
		}
	}
}

// The digest must be taken over the RAW scope key. Hashing the already-truncated value would
// reintroduce exactly the collision the hash exists to prevent, and the mistake is invisible in a
// diff -- both versions "add a hash suffix".
func TestTheDigestIsTakenOverTheRawInputNotTheTruncation(t *testing.T) {
	a := scope.Scope{ProjectID: strings.Repeat("p", 30), ClusterName: strings.Repeat("c", 40), Namespace: "alpha"}
	b := scope.Scope{ProjectID: strings.Repeat("p", 30), ClusterName: strings.Repeat("c", 40), Namespace: "beta"}
	ra, rb := RenderScope(a), RenderScope(b)
	if ra == rb {
		t.Fatalf("two scopes sharing a >63-byte prefix rendered identically: %q", ra)
	}
	if !strings.HasPrefix(ra, Sanitize(ScopeKey(a))[:prefixLen]) {
		t.Errorf("%q does not carry the readable prefix of its scope", ra)
	}
}

func TestForStampsExactlyTheFiveKeysOfSpec08(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "payments-agent", Namespace: "payments"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:      agentv1alpha1.TierDeveloperTeam,
			Scope:     &agentv1alpha1.ScopeSpec{ProjectID: "acme-prod", ClusterName: "us-east4-a", Namespace: "payments"},
			ParentRef: &agentv1alpha1.ParentRefSpec{Name: "acme-cluster-admin"},
		},
	}
	got := For(agent, RoleReader)
	want := map[string]string{
		"kube-agents/tier":   "developer-team",
		"kube-agents/scope":  "acme-prod.us-east4-a.payments",
		"kube-agents/parent": "acme-cluster-admin",
		"kube-agents/role":   "reader",
		"kube-agents/agent":  "payments-agent",
	}
	if len(got) != len(want) {
		t.Fatalf("For stamped %d labels, want %d: %v", len(got), len(want), got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("label %s = %q, want %q", k, got[k], v)
		}
	}
	// The literal key strings are asserted above rather than compared against the constants, so
	// that renaming a constant cannot silently rename the key an admission policy selects on.
	for _, k := range []string{Tier, Scope, Parent, Role, Agent} {
		if _, ok := want[k]; !ok {
			t.Errorf("constant %q is not one of the five keys 08 §2.5 names", k)
		}
	}
}

func TestForOnAPlatformAgentStampsAnEmptyParentRatherThanOmittingIt(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierPlatform,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: "acme-prod"},
		},
	}
	got := For(agent, RoleReader)
	v, present := got[Parent]
	if !present {
		t.Fatal("the parent label must be present and empty on a root agent: absent means " +
			"\"the controller did not stamp it\", which is a different fact")
	}
	if v != "" {
		t.Errorf("parent = %q, want empty", v)
	}
}

// An Agent stored before the CRD's tier default existed has an empty spec.tier. Stamping
// `kube-agents/tier: ""` on its pods would drop them out of every per-tier egress NetworkPolicy
// (03 §10) and out of vap-agent-scope's selector (03 §4.2) -- the pod would run with no tier policy
// applied, and no component would report an error, because a selector matching nothing is
// indistinguishable from a selector over a legitimately empty set.
func TestForStampsTheEffectiveTierNotTheRawSpecField(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "legacy"},
		Spec:       agentv1alpha1.AgentSpec{Tier: ""},
	}
	if got := For(agent, RoleReader)[Tier]; got != string(agentv1alpha1.TierPlatform) {
		t.Errorf("tier = %q, want %q -- an empty spec.tier must default the way the rest of the "+
			"operator defaults it, not render an empty label", got, agentv1alpha1.TierPlatform)
	}
}

// The zero scope renders empty, and does so by an explicit rule rather than by falling through.
// Hashing it would produce `scope-e3b0c44298` -- a value that reads like a real scope in
// `kubectl get pods -L kube-agents/scope` and that an operator would go looking for.
func TestTheZeroScopeRendersEmptyRatherThanHashingToSomethingThatLooksReal(t *testing.T) {
	if got := RenderScope(scope.Scope{}); got != "" {
		t.Errorf("RenderScope(zero) = %q, want the empty string", got)
	}
	// And it is still injective: nothing non-empty may reach the empty rendering. The corpus sweep
	// covers this in general; this pins the specific boundary, including a scope whose every level
	// sanitizes away to nothing.
	for _, s := range []scope.Scope{
		{ProjectID: "a"},
		{ProjectID: "---"},
		{ProjectID: "___"},
		{ProjectID: "\x00"},
	} {
		if got := RenderScope(s); got == "" {
			t.Errorf("RenderScope(%+v) = %q, but only the zero scope may render empty", s, got)
		}
	}
}

func TestForCarriesTheRoleThroughUnchanged(t *testing.T) {
	agent := &agentv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "a"}}
	if got := For(agent, RoleActor)[Role]; got != "actor" {
		t.Errorf("role = %q, want actor", got)
	}
	if got := For(agent, RoleReader)[Role]; got != "reader" {
		t.Errorf("role = %q, want reader", got)
	}
	// An unexpected role is passed through rather than corrected. Silently rewriting it would hide
	// the caller's bug behind a pod that admission then treats as something it is not.
	if got := For(agent, "broker")[Role]; got != "broker" {
		t.Errorf("role = %q, want the caller's value passed through", got)
	}
}

func TestForOnANilAgentReturnsNoLabelsRatherThanPanicking(t *testing.T) {
	if got := For(nil, RoleReader); len(got) != 0 {
		t.Errorf("For(nil) = %v, want an empty map", got)
	}
}

// Every rendered VALUE must be legal, and so must every KEY: a malformed key is silently dropped by
// some clients and rejected by the API server, and either way the policy that selects on it stops
// selecting.
func TestEveryKeyIsALegalQualifiedName(t *testing.T) {
	keyRE := regexp.MustCompile(`^kube-agents/[a-z][a-z0-9-]*$`)
	for _, k := range []string{Tier, Scope, Parent, Role, Agent} {
		if errs := validation.IsQualifiedName(k); len(errs) > 0 {
			t.Errorf("%q is not a legal label key: %v", k, errs)
		}
		if !keyRE.MatchString(k) {
			t.Errorf("%q is not in the kube-agents/ namespace with a lowercase name", k)
		}
	}
}

func TestSanitizeLowercasesRatherThanReplacing(t *testing.T) {
	if got := Sanitize("Payments"); got != "payments" {
		t.Errorf("Sanitize(\"Payments\") = %q, want \"payments\"", got)
	}
	if got := Sanitize("a b"); got != "a-b" {
		t.Errorf("Sanitize(\"a b\") = %q, want \"a-b\"", got)
	}
	if got := Sanitize("--trim--"); got != "trim" {
		t.Errorf("Sanitize(\"--trim--\") = %q, want \"trim\"", got)
	}
}

func TestScopeKeyDropsEmptyLevelsRatherThanLeavingHoles(t *testing.T) {
	if got := ScopeKey(scope.Scope{ProjectID: "p"}); got != "p" {
		t.Errorf("ScopeKey = %q, want \"p\" -- a trailing \"p..\" would render two different "+
			"depths to values that differ only in punctuation", got)
	}
	if got := ScopeKey(scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "n"}); got != "p.c.n" {
		t.Errorf("ScopeKey = %q, want \"p.c.n\"", got)
	}
	if got := ScopeKey(scope.Scope{}); got != "" {
		t.Errorf("ScopeKey(zero) = %q, want empty", got)
	}
}
