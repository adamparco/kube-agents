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
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// Selector is a label-selected set of targets, as submitted in an envelope.
type Selector struct {
	Group     string
	Version   string
	Kind      string
	Namespace string
	// MatchLabels is the equality-based selector. An EMPTY map matches everything in the namespace
	// and is refused -- see Expand.
	MatchLabels map[string]string
}

// Key is the stable identity of a selector, used to memoize its expansion.
func (s Selector) Key() string {
	keys := make([]string, 0, len(s.MatchLabels))
	for k := range s.MatchLabels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "%s=%s,", k, s.MatchLabels[k])
	}
	g := s.Group
	if g == "" {
		g = "core"
	}
	return fmt.Sprintf("%s/%s/%s/%s/[%s]", g, s.Version, s.Kind, s.Namespace, b.String())
}

// Lister resolves a selector against live cluster state.
type Lister interface {
	List(ctx context.Context, sel Selector) ([]agentv1alpha1.TargetRef, error)
}

// Expansion is a selector resolved to a fixed set of concrete targets at a fixed instant.
//
// 04 §1 requires the fan-out to be expanded ONCE, before classification, against live state. The
// reason is not efficiency. A selector re-resolved between classification and execution can name a
// different set: an object created in the interval is mutated by an action that was never
// classified against it, and an object deleted in the interval leaves a classified target that no
// longer exists. The classifier's verdict is about a SET, and a set that keeps changing has no
// verdict.
//
// The type carries that guarantee structurally: there is no exported way to add a ref to an
// Expansion, `Refs` hands back a copy, and everything downstream of here takes `[]TargetRef` --
// nothing but Expander ever holds a Selector.
type Expansion struct {
	selector Selector
	refs     []agentv1alpha1.TargetRef
	at       time.Time
}

// Selector is the selector this expansion came from.
func (e *Expansion) Selector() Selector { return e.selector }

// At is when live state was read.
func (e *Expansion) At() time.Time { return e.at }

// Len is the number of targets.
func (e *Expansion) Len() int { return len(e.refs) }

// Refs returns a COPY of the resolved targets. A caller that appends to the result changes its own
// slice and nothing else.
func (e *Expansion) Refs() []agentv1alpha1.TargetRef {
	out := make([]agentv1alpha1.TargetRef, len(e.refs))
	copy(out, e.refs)
	return out
}

// MaxFanout bounds how many objects one envelope may touch through a selector.
//
// An unbounded fan-out makes blast radius a property of whatever happens to be labelled, which is
// the one input the classifier cannot reason about: `patch Deployment app=web` is routine at three
// objects and a cluster-wide outage at three hundred. The bound is a refusal, not a truncation --
// silently acting on the first N is worse than both alternatives.
const MaxFanout = 50

// Expander resolves selectors exactly once per action.
//
// It is scoped to one action: construct it, expand every selector in the envelope, then discard it.
// Sharing one across actions would memoize a stale set into an action that never read live state.
type Expander struct {
	Lister Lister
	Now    func() time.Time

	mu   sync.Mutex
	done map[string]*Expansion
	// calls counts Lister invocations. Exported through ListCalls so the "expanded once" property
	// is observable rather than merely intended.
	calls int
}

// NewExpander returns an Expander for one action.
func NewExpander(l Lister) *Expander {
	return &Expander{Lister: l, done: map[string]*Expansion{}}
}

// ListCalls is how many times live state was actually read.
func (e *Expander) ListCalls() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.calls
}

// Expand resolves a selector against live state, or returns the answer it already has.
//
// A second call for the same selector returns the FIRST expansion, unchanged and with its original
// timestamp. It does not re-read the cluster, and it does not error: erroring would push every
// caller into caching the result itself, and a caching rule enforced at each of N call sites is a
// rule that holds at N-1 of them.
func (e *Expander) Expand(ctx context.Context, sel Selector) (*Expansion, error) {
	if e.Lister == nil {
		return nil, fmt.Errorf("expander has no Lister: a selector cannot be resolved without live state")
	}
	if len(sel.MatchLabels) == 0 {
		return nil, fmt.Errorf("selector for %s/%s in namespace %q has no labels: an empty selector "+
			"matches every object of the kind, which is a fan-out nobody wrote down",
			sel.Kind, sel.Version, sel.Namespace)
	}
	if sel.Kind == "" || sel.Version == "" {
		return nil, fmt.Errorf("selector must name a kind and a version, got %q/%q", sel.Version, sel.Kind)
	}

	key := sel.Key()

	e.mu.Lock()
	if exp, ok := e.done[key]; ok {
		e.mu.Unlock()
		return exp, nil
	}
	e.mu.Unlock()

	refs, err := e.Lister.List(ctx, sel)
	if err != nil {
		return nil, fmt.Errorf("expanding selector %s: %w", key, err)
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	// Re-check under the lock: two goroutines expanding the same selector must not produce two
	// different answers, and the loser's List result is discarded rather than returned.
	if exp, ok := e.done[key]; ok {
		return exp, nil
	}
	e.calls++

	if len(refs) > MaxFanout {
		return nil, fmt.Errorf("selector %s expands to %d objects, above the %d-object fan-out limit: "+
			"refused rather than truncated", key, len(refs), MaxFanout)
	}
	if len(refs) == 0 {
		return nil, fmt.Errorf("selector %s matched no objects: an action with no targets is not a "+
			"no-op to journal, it is an envelope whose author believed something was there", key)
	}

	sorted := make([]agentv1alpha1.TargetRef, len(refs))
	copy(sorted, refs)
	// Deterministic order: the executed diff, the undo plan and every golden are indexed by
	// position in spec.targets, and a List whose order varies between calls would make those
	// indices mean different objects on a replay.
	sort.Slice(sorted, func(i, j int) bool {
		if sorted[i].Namespace != sorted[j].Namespace {
			return sorted[i].Namespace < sorted[j].Namespace
		}
		return sorted[i].Name < sorted[j].Name
	})

	at := time.Now()
	if e.Now != nil {
		at = e.Now()
	}
	exp := &Expansion{selector: sel, refs: sorted, at: at}
	e.done[key] = exp
	return exp, nil
}
