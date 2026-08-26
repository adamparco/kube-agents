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

package policy

import (
	"fmt"
	"sort"
	"strings"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// Snapshot is one immutable reading of the cluster's ChangePolicy set, already converted, already
// validated, and with the classifier for it already built.
//
// Built once per observation rather than once per submission. Two reasons, and the second is the
// one that matters. Building it per submission would put `classify.New` -- which validates the code
// floor and every policy rule -- on the hot path of the one process an agent waits on. And it would
// mean two operations in the same envelope could, in principle, classify against different policy
// sets, so "the envelope is as risky as its riskiest operation" would be a statement about a rule
// table that changed underneath it.
type Snapshot struct {
	names      []string
	classifier *classify.Classifier
	at         time.Time
}

// Names are the bound policies, sorted, as they appear in `classification.policySources[]`.
func (s *Snapshot) Names() []string {
	if s == nil {
		return nil
	}
	return append([]string(nil), s.names...)
}

// Classifier is the classifier for this policy set. Never nil on a snapshot Build returned without
// an error.
func (s *Snapshot) Classifier() *classify.Classifier {
	if s == nil {
		return nil
	}
	return s.classifier
}

// ObservedAt is when the API server answered the read this snapshot was built from -- NOT when a
// policy last changed. The distinction is the whole of the staleness rule: a quiet cluster whose
// watch died looks identical to a quiet cluster, if the timestamp records changes.
func (s *Snapshot) ObservedAt() time.Time {
	if s == nil {
		return time.Time{}
	}
	return s.at
}

// Build converts every policy that binds `a` into the classifier's rule table.
//
// Ordered by policy name, deterministically. `classification.policySources[]` lands in an
// ActionRecord that a human reads during an incident and that a check compares against a fixture;
// map iteration order there would make two identical actions produce two different records.
//
// Every failure is returned, and returning one means the caller has NO classifier rather than a
// partial one. This is the single most important line in the package. The classifier maxes over its
// sources, so a policy that fails to convert and gets skipped does not produce a slightly-wrong
// classification -- it produces the classification the operator wrote the policy to prevent, with a
// `policySources` list that does not mention the policy, in a record that looks entirely normal.
// There is no failure of this function whose safe handling is "carry on with the rest".
func Build(policies []*agentv1alpha1.ChangePolicy, a Agent, history classify.ActionHistory, at time.Time) (*Snapshot, error) {
	// The agent's OWN scope gets the same well-formedness refusal the policies get, and for the
	// mirror-image reason. There the hole was in the outer scope and read as a wildcard that binds
	// too much; here it is in the inner one, and `scope.Contains` walks the outer's narrowed levels
	// against an inner that has nothing at that level -- so a policy scoped to project `p` does not
	// bind an agent whose scope is `{namespace: n}` with the project missing. That is a binding
	// LOST, and since a ChangePolicy can only tighten, a lost binding is the loosening direction:
	// the broker classifies lower than the operator wrote, and the record's `policySources` omits
	// the policy without a word.
	//
	// The ZERO scope is deliberately allowed through. A platform Agent may legally carry no scope
	// at all -- the webhook's validateScopeAndParent returns early for that tier, "projectId is
	// conventional but scope may be nil here" -- so `Scope{}` is a real identity meaning "narrows
	// nothing", and it is well-formed. It binds only the policies that themselves narrow nothing,
	// which is the correct answer for a fleet-wide agent and the wrong one for an agent whose CR
	// could not be read. Those two are the same VALUE and different FACTS, and it is the identity
	// resolver's job to tell them apart by returning an error rather than a zero Agent; see
	// SourceConfig.Identity. Refusing the zero scope here instead would make the platform tier
	// unserviceable in order to catch a case this function cannot see.
	if !a.Scope.IsWellFormed() {
		return nil, fmt.Errorf(
			"this broker's own agent scope %+v is not a prefix narrowing (an empty level above a non-empty one). "+
				"scope.Contains stops at the first level a scope does not narrow, so a policy naming the missing level would not bind this agent at all. "+
				"A ChangePolicy only tightens, so a policy that fails to bind is a classification lower than the operator wrote -- refusing to classify is the only safe answer",
			a.Scope)
	}

	bound := make([]*agentv1alpha1.ChangePolicy, 0, len(policies))
	for _, cp := range policies {
		if cp == nil {
			continue
		}
		// Checked before Binds and for every policy, not only the ones that bind. A malformed scope
		// is precisely a scope whose containment answer cannot be trusted, so "it did not bind me"
		// is not a finding this broker is entitled to act on.
		if bad := IllFormedScopes(cp); len(bad) > 0 {
			return nil, fmt.Errorf(
				"ChangePolicy %q: spec.agentSelector.scopes%s is not a prefix narrowing (an empty level above a non-empty one). "+
					"scope.Contains stops at the first level a scope does not narrow, so such an entry matches a cluster or namespace of that name in EVERY project. "+
					"This broker refuses to classify against a policy set containing one rather than decide whether it binds",
				cp.Name, indexList(bad))
		}
		if Binds(cp, a) {
			bound = append(bound, cp)
		}
	}
	sort.Slice(bound, func(i, j int) bool { return bound[i].Name < bound[j].Name })

	sets := make([]classify.RuleSet, 0, len(bound))
	names := make([]string, 0, len(bound))
	for _, cp := range bound {
		// ValidateChangeRule, rule by rule, is the same function the admission webhook runs. A rule
		// the broker would refuse and admission accepted is a policy that exists in the cluster and
		// is not in effect (see classify.ValidateChangeRule); running the identical function on both
		// sides is what makes that impossible rather than unlikely.
		for i := range cp.Spec.Rules {
			if err := classify.ValidateChangeRule(&cp.Spec.Rules[i]); err != nil {
				return nil, fmt.Errorf("ChangePolicy %q: rules[%d]: %w", cp.Name, i, err)
			}
		}
		rs, err := classify.FromChangePolicy(cp)
		if err != nil {
			return nil, err
		}
		sets = append(sets, rs)
		names = append(names, cp.Name)
	}

	c, err := classify.New(sets, history)
	if err != nil {
		return nil, fmt.Errorf("building the classifier over %d bound policies (%s): %w",
			len(names), strings.Join(names, ", "), err)
	}
	return &Snapshot{names: names, classifier: c, at: at}, nil
}

func indexList(idx []int) string {
	parts := make([]string, len(idx))
	for i, n := range idx {
		parts[i] = fmt.Sprintf("[%d]", n)
	}
	return strings.Join(parts, "")
}
