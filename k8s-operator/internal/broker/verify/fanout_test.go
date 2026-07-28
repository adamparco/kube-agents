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

package verify

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeLister answers a selector with a fixed set, and can change its answer between calls so a test
// can prove the second call never happened.
type fakeLister struct {
	mu      sync.Mutex
	answers [][]agentv1alpha1.TargetRef
	err     error
	calls   int
}

func (f *fakeLister) List(_ context.Context, _ Selector) ([]agentv1alpha1.TargetRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	i := f.calls - 1
	if i >= len(f.answers) {
		i = len(f.answers) - 1
	}
	return f.answers[i], nil
}

func refs(names ...string) []agentv1alpha1.TargetRef {
	out := make([]agentv1alpha1.TargetRef, 0, len(names))
	for _, n := range names {
		out = append(out, agentv1alpha1.TargetRef{
			Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod", Name: n,
		})
	}
	return out
}

func webSelector() Selector {
	return Selector{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod",
		MatchLabels: map[string]string{"app": "web", "tier": "front"}}
}

// TestSelectorIsExpandedExactlyOnce is the 04 §1 property. The lister's answer CHANGES between
// calls, so an implementation that re-resolved would not merely be slower -- it would return a
// different set, which is the actual hazard.
func TestSelectorIsExpandedExactlyOnce(t *testing.T) {
	ctx := context.Background()
	l := &fakeLister{answers: [][]agentv1alpha1.TargetRef{
		refs("api", "web"),
		refs("api", "web", "worker"), // someone labelled a third object in the interval
	}}
	e := NewExpander(l)
	e.Now = func() time.Time { return base }

	first, err := e.Expand(ctx, webSelector())
	if err != nil {
		t.Fatalf("first expand: %v", err)
	}
	second, err := e.Expand(ctx, webSelector())
	if err != nil {
		t.Fatalf("second expand: %v", err)
	}

	if e.ListCalls() != 1 {
		t.Errorf("live state was read %d times, want 1", e.ListCalls())
	}
	if l.calls != 1 {
		t.Errorf("the lister was called %d times, want 1", l.calls)
	}
	if second.Len() != 2 {
		t.Errorf("the second expansion has %d refs, want the first expansion's 2 — the classifier's "+
			"verdict is about a set, and this one grew underneath it", second.Len())
	}
	if !second.At().Equal(first.At()) {
		t.Error("the memoized expansion was restamped; its timestamp is when live state was read")
	}
	if first != second {
		t.Error("two Expand calls returned different Expansions for one selector")
	}
}

func TestExpandIsDeterministicallyOrdered(t *testing.T) {
	ctx := context.Background()
	l := &fakeLister{answers: [][]agentv1alpha1.TargetRef{refs("web", "api", "worker")}}
	e := NewExpander(l)

	exp, err := e.Expand(ctx, webSelector())
	if err != nil {
		t.Fatalf("expand: %v", err)
	}
	got := []string{}
	for _, r := range exp.Refs() {
		got = append(got, r.Name)
	}
	want := []string{"api", "web", "worker"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("order = %v, want %v — the executed diff, the undo plan and every golden are "+
			"indexed by position in spec.targets", got, want)
	}
}

func TestExpansionRefsIsACopy(t *testing.T) {
	ctx := context.Background()
	e := NewExpander(&fakeLister{answers: [][]agentv1alpha1.TargetRef{refs("api", "web")}})
	exp, err := e.Expand(ctx, webSelector())
	if err != nil {
		t.Fatalf("expand: %v", err)
	}

	got := exp.Refs()
	got[0].Name = "rewritten"
	got = append(got, agentv1alpha1.TargetRef{Name: "smuggled"})
	_ = got

	again := exp.Refs()
	if len(again) != 2 || again[0].Name != "api" {
		t.Fatalf("mutating the returned slice changed the expansion: %+v", again)
	}
	if exp.Len() != 2 {
		t.Errorf("Len = %d after a caller appended to its own copy", exp.Len())
	}
}

func TestExpandRefusesAnEmptySelector(t *testing.T) {
	ctx := context.Background()
	l := &fakeLister{answers: [][]agentv1alpha1.TargetRef{refs("api")}}
	e := NewExpander(l)

	sel := webSelector()
	sel.MatchLabels = nil
	if _, err := e.Expand(ctx, sel); err == nil {
		t.Fatal("a selector with no labels was expanded; it matches everything of the kind")
	}
	sel.MatchLabels = map[string]string{}
	if _, err := e.Expand(ctx, sel); err == nil {
		t.Fatal("an empty (non-nil) label map was expanded")
	}
	if l.calls != 0 {
		t.Errorf("a refused selector still read live state (%d calls)", l.calls)
	}
}

func TestExpandRefusesAnIncompleteSelector(t *testing.T) {
	ctx := context.Background()
	e := NewExpander(&fakeLister{answers: [][]agentv1alpha1.TargetRef{refs("api")}})
	for _, sel := range []Selector{
		{Version: "v1", MatchLabels: map[string]string{"app": "web"}},      // no kind
		{Kind: "Deployment", MatchLabels: map[string]string{"app": "web"}}, // no version
	} {
		if _, err := e.Expand(ctx, sel); err == nil {
			t.Errorf("selector %+v was expanded", sel)
		}
	}
}

// TestExpandRefusesRatherThanTruncates is the MaxFanout control. Truncation is the failure this
// bound exists to avoid, so the assertion is on the error, not on a shortened result.
func TestExpandRefusesRatherThanTruncates(t *testing.T) {
	ctx := context.Background()
	many := make([]agentv1alpha1.TargetRef, 0, MaxFanout+1)
	for i := 0; i <= MaxFanout; i++ {
		many = append(many, agentv1alpha1.TargetRef{
			Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod",
			Name: fmt.Sprintf("web-%03d", i),
		})
	}
	e := NewExpander(&fakeLister{answers: [][]agentv1alpha1.TargetRef{many}})

	exp, err := e.Expand(ctx, webSelector())
	if err == nil {
		t.Fatalf("a %d-object fan-out was accepted, expanding to %d", len(many), exp.Len())
	}
	if exp != nil {
		t.Errorf("a refused expansion still returned %d refs", exp.Len())
	}
	if !strings.Contains(err.Error(), "refused rather than truncated") {
		t.Errorf("the error does not say the fan-out was refused: %v", err)
	}

	// Exactly at the bound is fine: an off-by-one here would silently narrow every envelope.
	e2 := NewExpander(&fakeLister{answers: [][]agentv1alpha1.TargetRef{many[:MaxFanout]}})
	if _, err := e2.Expand(ctx, webSelector()); err != nil {
		t.Errorf("exactly %d objects was refused: %v", MaxFanout, err)
	}
}

func TestExpandRefusesAZeroMatchSelector(t *testing.T) {
	ctx := context.Background()
	e := NewExpander(&fakeLister{answers: [][]agentv1alpha1.TargetRef{{}}})
	if _, err := e.Expand(ctx, webSelector()); err == nil {
		t.Fatal("a selector matching nothing was accepted as a zero-target action")
	}
}

func TestExpandSurfacesListerErrors(t *testing.T) {
	ctx := context.Background()
	e := NewExpander(&fakeLister{err: errors.New("the API server said no")})
	if _, err := e.Expand(ctx, webSelector()); err == nil {
		t.Fatal("a failed List produced an expansion")
	}

	var nilLister *Expander = &Expander{done: map[string]*Expansion{}}
	if _, err := nilLister.Expand(ctx, webSelector()); err == nil {
		t.Fatal("an Expander with no Lister produced an expansion")
	}
}

func TestSelectorKeyIsOrderIndependent(t *testing.T) {
	// Go map iteration is randomized, so a Key that walked the map unsorted would memoize under a
	// different string on some runs and re-read live state -- the exact property this file exists
	// to hold.
	a := Selector{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod",
		MatchLabels: map[string]string{"app": "web", "tier": "front", "env": "prod"}}
	b := Selector{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod",
		MatchLabels: map[string]string{"env": "prod", "app": "web", "tier": "front"}}
	if a.Key() != b.Key() {
		t.Errorf("same selector, different keys:\n  %s\n  %s", a.Key(), b.Key())
	}

	c := a
	c.MatchLabels = map[string]string{"app": "api", "tier": "front", "env": "prod"}
	if a.Key() == c.Key() {
		t.Error("two different selectors share a key")
	}
}

func TestExpandIsSafeUnderConcurrency(t *testing.T) {
	ctx := context.Background()
	l := &fakeLister{answers: [][]agentv1alpha1.TargetRef{refs("api", "web"), refs("api")}}
	e := NewExpander(l)

	var wg sync.WaitGroup
	got := make([]*Expansion, 16)
	for i := range got {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			exp, err := e.Expand(ctx, webSelector())
			if err != nil {
				t.Errorf("expand: %v", err)
				return
			}
			got[i] = exp
		}(i)
	}
	wg.Wait()

	if e.ListCalls() != 1 {
		t.Errorf("ListCalls = %d under concurrency, want 1", e.ListCalls())
	}
	for i, exp := range got {
		if exp != got[0] {
			t.Fatalf("goroutine %d got a different Expansion", i)
		}
	}
}
