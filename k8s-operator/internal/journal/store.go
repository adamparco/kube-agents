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

package journal

import (
	"context"
	"fmt"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentlabels"
)

// The journal store (`C-JS`) is the ActionRecord custom resource itself, not a service in front of
// one (05 §1.2). The decision is worth restating where the code lives, because "store" reads like a
// database and the absence of one is the point: the broker fails closed when it cannot journal, so
// the journal's availability IS the system's write availability, and etcd is already required and
// already up. An external database would add a second thing that must be up before a pod can be
// restarted, with its own credential, its own backup story, and its own hole in a default-deny
// egress policy.
//
// What this file adds on top of a plain client is the part that must not vary between callers: the
// label set the whole system queries by, and the fail-closed Create.

// Label keys on an ActionRecord (06 §4.3). They exist so the common questions -- what did this agent
// do, what is still pending, what did this chain touch -- are label selectors and not full scans.
const (
	// TierLabel is the agent tier: platform, cluster-admin, developer-team. Same KEY as the one
	// internal/agentlabels stamps on workloads, and taken from there rather than respelled, because
	// a record whose tier label differs from its agent's pods by one character joins to nothing.
	TierLabel = agentlabels.Tier

	// ScopeLabel is the agent's scope LEAF, e.g. the namespace for a developer-team agent. The key
	// is shared with agentlabels.Scope; the VALUE is not the same rendering.
	//
	// 08 §2.5 defines this key on a workload as the DNS-safe rendering of the whole scope key
	// (`<project>.<cluster>.<ns>`, hash-suffixed when long). 06 §4.3's ActionRecord examples and the
	// 06 §5.1 ServiceAccount table use the same key for the leaf alone. Those are different objects
	// so they do not contradict, but the leaf is NOT injective across a fleet -- two clusters each
	// holding a `team-x` namespace produce the same value -- so this label is safe as an index and
	// must never be treated as an identity. Whether the two meanings should be reconciled is a spec
	// question with a real blast radius (03 §4.2 compares a pod's value against its SA's) and no
	// check pointed at it; it is recorded as an open item rather than decided here. Nothing in this
	// package may render a scope value through agentlabels.RenderScope until that is settled --
	// changing the value silently would break every existing selector.
	ScopeLabel = agentlabels.Scope
	// RiskClassLabel mirrors spec.classification.class.
	RiskClassLabel = "kube-agents/risk-class"
	// StatusLabel mirrors status.phase. It is a LABEL as well as a status field so the ChatOps
	// reporter can watch one phase without watching every record; the reconciler keeps the two in
	// step, and any drift between them is a bug in this package rather than in a caller.
	StatusLabel = "kube-agents/status"
	// TriggerLabel mirrors spec.trigger.source.
	TriggerLabel = "kube-agents/trigger"
	// ChainIDLabel is the lower-cased delegation chain id. Selecting on it reconstructs a
	// multi-agent chain in one query, which is the only cheap way to answer "what did that one
	// request actually do across three agents".
	ChainIDLabel = "kube-agents/chain-id"
	// UndoOfLabel is present ONLY on an undo record.
	UndoOfLabel = "kube-agents/undo-of"

	// ActionIDLabel is the label an actor identity must put on every object it WRITES -- not on the
	// record. It is the join key for V-BRK-003, and admission rejects a write from an actor
	// identity that lacks it (05 §1.1).
	ActionIDLabel = "kube-agents/action-id"

	// ContestedAnnotation is stamped on a TARGET object, not on the record, and it is ADVISORY
	// (06 §4.4). Its value is the originating action id.
	//
	// An annotation and not a label because nothing selects on it: the authoritative refusal comes
	// from the broker's in-memory index and from ActionRecord.status.contested, for the reason
	// 06 §4.4 gives outright -- a deleted object cannot hold an annotation, and the commonest
	// contested case is a human undoing a create. This exists so a person running `kubectl get -o
	// yaml` on a live object learns why the agent stopped touching it, and it must never be the
	// thing that decides.
	ContestedAnnotation = "kube-agents/contested"
)

// Store is the journal's write and read surface. Everything the broker, the undo controller and the
// reporter do to the journal goes through here, so that the labels above are applied in exactly one
// place and a caller cannot half-populate them.
type Store struct {
	client client.Client
	// Sink holds bodies too large to inline. Nil is legal -- and Snapshot then refuses any
	// oversized body rather than silently inlining it.
	Sink BlobSink
}

// NewStore returns a Store over the given client.
func NewStore(c client.Client, sink BlobSink) *Store { return &Store{client: c, Sink: sink} }

// Labels builds the full label set for a record. Label values are constrained to 63 characters of
// [A-Za-z0-9._-], so every value here is truncated and sanitized rather than trusted: an
// agentIdentity contains slashes, and a scope leaf can be arbitrarily long. A rejected Create
// because a label was one character too long would fail the action, and failing an action over a
// label is the wrong trade -- the authoritative values live in spec, and these are an index.
func Labels(ar *agentv1alpha1.ActionRecord) map[string]string {
	l := map[string]string{
		TierLabel:      labelValue(tierOf(ar.Spec.AgentIdentity)),
		ScopeLabel:     labelValue(scopeLeafOf(ar.Spec.AgentIdentity)),
		RiskClassLabel: labelValue(string(ar.Spec.Classification.Class)),
		TriggerLabel:   labelValue(string(ar.Spec.Trigger.Source)),
		ChainIDLabel:   labelValue(strings.ToLower(ar.Spec.Trigger.ChainID)),
	}
	if ar.Status.Phase != "" {
		l[StatusLabel] = labelValue(string(ar.Status.Phase))
	}
	if ar.Spec.Trigger.UndoOf != "" {
		l[UndoOfLabel] = labelValue(strings.ToLower(ar.Spec.Trigger.UndoOf))
	}
	return l
}

// tierOf and scopeLeafOf split an agentIdentity like
// `developer-team/my-project/cluster-a/team-x` into its index-worthy parts. Parsing rather than
// carrying two more spec fields keeps the identity a single authoritative string; the identity is
// what RBAC and the broker key off, and a second copy would be a second thing to disagree.
func tierOf(identity string) string {
	tier, _, _ := strings.Cut(identity, "/")
	return tier
}

func scopeLeafOf(identity string) string {
	i := strings.LastIndex(identity, "/")
	if i < 0 {
		return ""
	}
	return identity[i+1:]
}

// labelValue coerces s into something a label will accept: legal characters only, legal first and
// last character, at most 63 bytes.
func labelValue(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
			b.WriteRune(r)
		default:
			b.WriteRune('-')
		}
	}
	out := b.String()
	if len(out) > 63 {
		out = out[:63]
	}
	out = strings.Trim(out, "-_.")
	return out
}

// Create writes the record and is the fail-closed point of 03 §6: if this returns an error the
// caller MUST NOT execute the action. The name is derived from the action id rather than generated,
// so a retried Create is an AlreadyExists on the SAME record instead of a duplicate journal entry
// for one action -- which is what makes the broker's retry safe without a distributed lock.
func (s *Store) Create(ctx context.Context, ar *agentv1alpha1.ActionRecord) error {
	if !ValidULID(ar.Spec.ActionID) {
		return fmt.Errorf("journal: spec.actionId %q is not a ULID; refusing to create a record that cannot be joined to its writes", ar.Spec.ActionID)
	}
	// 06 §4.3: a record may only be born in a phase something could have observed. Refused here
	// rather than at the first SetPhase, because Create is the fail-closed point -- a caller that
	// gets nil back proceeds to execute, and a record created as `Verified` would already be a
	// journal entry claiming an outcome for an action that has not started.
	if err := agentv1alpha1.ValidateActionPhaseTransition("", ar.Status.Phase); err != nil {
		return fmt.Errorf("journal: refusing to create ActionRecord for %s: %w (the action must not execute -- fail closed, 03 §6)", ar.Spec.ActionID, err)
	}
	ar.Name = RecordName(ar.Spec.ActionID)
	if ar.Labels == nil {
		ar.Labels = map[string]string{}
	}
	for k, v := range Labels(ar) {
		ar.Labels[k] = v
	}
	want := ar.Status.Phase
	if err := s.client.Create(ctx, ar); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// Idempotent by construction: the same action id names the same record. Report it as
			// such so the caller can proceed rather than treating a safe retry as a journal failure
			// and refusing an action that is already journaled.
			//
			// Deliberately does NOT re-assert the phase. The existing record has been through the
			// lifecycle since; a retried Create that stamped its own idea of the starting phase back
			// onto it would be a transition nothing validated, taken by the one code path whose
			// whole contract is "this changed nothing".
			return nil
		}
		return fmt.Errorf("journal: create ActionRecord %s/%s: %w (the action must not execute -- fail closed, 03 §6)", ar.Namespace, ar.Name, err)
	}
	// `status` is a subresource, so the Create above sent the object and the API server dropped the
	// status block: `Labels[StatusLabel]` landed and `status.phase` did not. That asymmetry is not
	// cosmetic -- 06 §4.3 makes `status.phase` authoritative and the label a derived index, and
	// leaving it empty inverted the two. Every parked record read back `status.phase: ""` while its
	// label said `PendingApproval`, so 06 §4.3's ChatOps row (`PendingApproval -> Pending/Rejected`,
	// and nothing else) had no `PendingApproval` to transition out of.
	if want == "" {
		return nil
	}
	ar.Status.Phase = want
	if err := s.client.Status().Update(ctx, ar); err != nil {
		return fmt.Errorf("journal: create ActionRecord %s/%s: recording initial phase %q: %w (the action must not execute -- fail closed, 03 §6)", ar.Namespace, ar.Name, want, err)
	}
	return nil
}

// Get fetches a record by action id from a namespace.
func (s *Store) Get(ctx context.Context, namespace, actionID string) (*agentv1alpha1.ActionRecord, error) {
	var ar agentv1alpha1.ActionRecord
	key := client.ObjectKey{Namespace: namespace, Name: RecordName(actionID)}
	if err := s.client.Get(ctx, key, &ar); err != nil {
		return nil, err
	}
	return &ar, nil
}

// SetPhase writes a phase transition to status and keeps the status LABEL in step, in one call, so
// the two cannot drift. It re-reads before writing: status conflicts are routine when the broker and
// a controller both touch a record, and a conflict loop is far cheaper than a lost transition.
func (s *Store) SetPhase(ctx context.Context, ar *agentv1alpha1.ActionRecord, phase agentv1alpha1.ActionPhase, message string) error {
	var live agentv1alpha1.ActionRecord
	if err := s.client.Get(ctx, client.ObjectKeyFromObject(ar), &live); err != nil {
		return fmt.Errorf("journal: re-read %s/%s before phase change: %w", ar.Namespace, ar.Name, err)
	}
	// Validated against the phase that was just re-read, never against the caller's stale copy.
	// Checking `ar.Status.Phase` would ask whether the transition was legal from a world that may
	// have moved -- which is the same class of mistake as a read-modify-write without a conflict
	// check, and it would let two writers each take a legal step into a pair that is not.
	if err := agentv1alpha1.ValidateActionPhaseTransition(live.Status.Phase, phase); err != nil {
		return fmt.Errorf("journal: refusing phase change on %s/%s: %w", ar.Namespace, ar.Name, err)
	}
	live.Status.Phase = phase
	live.Status.Message = message
	if err := s.client.Status().Update(ctx, &live); err != nil {
		return fmt.Errorf("journal: set phase %q on %s/%s: %w", phase, ar.Namespace, ar.Name, err)
	}
	// The label is a separate write because status and metadata are separate subresources. It is
	// best-effort ordering, never best-effort truth: status.phase above is authoritative, and the
	// reconciler repairs the label if this second write is lost.
	if live.Labels == nil {
		live.Labels = map[string]string{}
	}
	if live.Labels[StatusLabel] == labelValue(string(phase)) {
		ar.Status = live.Status
		return nil
	}
	live.Labels[StatusLabel] = labelValue(string(phase))
	if err := s.client.Update(ctx, &live); err != nil {
		return fmt.Errorf("journal: sync %s label on %s/%s: %w", StatusLabel, ar.Namespace, ar.Name, err)
	}
	ar.Status = live.Status
	ar.Labels = live.Labels
	return nil
}

// List returns records in a namespace matching the given label selectors. A nil selector lists all.
func (s *Store) List(ctx context.Context, namespace string, match map[string]string) ([]agentv1alpha1.ActionRecord, error) {
	var list agentv1alpha1.ActionRecordList
	opts := []client.ListOption{client.InNamespace(namespace)}
	if len(match) > 0 {
		opts = append(opts, client.MatchingLabels(match))
	}
	if err := s.client.List(ctx, &list, opts...); err != nil {
		return nil, fmt.Errorf("journal: list ActionRecords in %q: %w", namespace, err)
	}
	return list.Items, nil
}

// Chain returns every record in a delegation chain, across namespaces. This is the query that
// answers "what did that one chat message actually do", and it is cross-namespace because a
// delegation crosses tiers by definition -- a platform agent's record and the developer-team record
// it caused do not share a namespace.
func (s *Store) Chain(ctx context.Context, chainID string) ([]agentv1alpha1.ActionRecord, error) {
	var list agentv1alpha1.ActionRecordList
	if err := s.client.List(ctx, &list, client.MatchingLabels{ChainIDLabel: labelValue(strings.ToLower(chainID))}); err != nil {
		return nil, fmt.Errorf("journal: list chain %q: %w", chainID, err)
	}
	return list.Items, nil
}
