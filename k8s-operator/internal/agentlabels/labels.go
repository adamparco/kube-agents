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

// Package agentlabels is the ONE place a `kube-agents/*` label key is spelled and the ONE place a
// label VALUE is rendered from a scope. Both halves of an Agent's pair (08 §2.5), the journal
// (06 §3), and every selector that pairs a pod to its ServiceAccount read from here.
//
// # Why a package rather than five string constants next to the renderer
//
// 08 §2.5 makes `kube-agents/role` "the admission policies'" input: `vap-agent-pod-hardening`
// asserts that a pod bound to an actor SA carries `role: actor`, and `vap-agent-scope` selects
// reader RBAC from actor RBAC. A label that a cluster policy authorizes on is not decoration -- if
// the controller and the policy disagree about the spelling of a key, the policy silently selects
// nothing and the assertion it was written to make is simply not made. Nothing fails; the guard is
// just gone. So the keys live in one importable place and the tests that matter assert the literal
// strings, so that a rename has to be a deliberate, visible edit in a file whose whole subject is
// that these strings are load-bearing.
//
// # Why the scope renderer is the interesting part (09 V-RUN-011)
//
// A scope key is `<project>.<cluster>.<namespace>`. A GCP project id is up to 30 characters, a GKE
// cluster name up to 40, a namespace up to 63: the concatenation routinely exceeds the 63-byte
// ceiling a label value has. Truncation alone is therefore the DEFAULT behaviour, not an edge case,
// and truncation alone is not injective:
//
//	acme-prod.us-east4-clusterrunninglongname.payments-api-frontend  -> first 63 bytes
//	acme-prod.us-east4-clusterrunninglongname.payments-api-backend   -> the SAME first 63 bytes
//
// Those are two different agents, in two different namespaces, with two different actor
// ServiceAccounts and two different blast radii. 08 §2.5 keys the pod-to-SA pinning selector, the
// NetworkPolicy, and the per-scope quota on this value. If they render to the same label then a
// selector meant for one agent's pods matches the other's, and the pinning that is supposed to make
// "this pod may hold this credential" checkable stops distinguishing the two credentials. 09
// V-RUN-011 says this outright: "a collision is an authority bug, not a cosmetic one".
//
// So RenderScope is built to be injective, and the property is arranged to be provable rather than
// merely tested:
//
//  1. A value that is already a legal, lowercase, short label passes through UNCHANGED -- distinct
//     inputs are then trivially distinct outputs, because the output IS the input.
//  2. Anything else -- too long, uppercase, or containing a character a label may not hold -- is
//     rendered as a truncated prefix plus `-` plus a 10-hex-character digest OF THE RAW INPUT. The
//     digest, not the prefix, is what carries the distinction.
//  3. The two sets cannot overlap, because a value that would LOOK hashed (`-[0-9a-f]{10}` at the
//     end) is pushed into set 2 even when it would otherwise have passed through. Without rule 3 a
//     literal scope of `a-0123456789` could collide with the hashed rendering of some long scope,
//     and the whole argument would rest on "that will not happen in practice".
//
// What is left is a 40-bit digest collision between two raw scopes that ALSO share a truncated
// prefix. That residual is stated rather than hidden, and the injectivity tests sweep for it; 40
// bits is chosen over 32 because the population is small but the consequence of a collision is an
// authority bug rather than a cosmetic one.
//
// Note what this package deliberately does NOT do: it never decides authority. The full,
// unnormalized scope stays authoritative in `spec.scope` (08 §2.5) and every containment decision
// goes through internal/scope. A label is a selector; treating one as an authorization input would
// mean authorizing on a lossy, truncated, hashed rendering.
package agentlabels

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	"k8s.io/apimachinery/pkg/util/validation"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// The five keys 08 §2.5 stamps on both Deployments and both pod templates, plus the two the journal
// (06 §3) shares with them. Spelled once, here.
const (
	// Tier selects every agent pod of a tier without naming agents: the per-tier egress
	// NetworkPolicy (03 §10) and `vap-agent-scope` (03 §4.2) both key on it.
	Tier = "kube-agents/tier"

	// Scope is the DNS-safe rendering of the scope key -- see RenderScope. Per-scope network and
	// quota policy, operator queries, and journal correlation key on it.
	Scope = "kube-agents/scope"

	// Parent is `parentRef.name`, empty for platform. Provisioning lineage and blast-radius queries.
	Parent = "kube-agents/parent"

	// Role is `reader` or `actor` and is the one label the inversion added. It is how a
	// cluster-wide policy says "no pod carrying role: reader may mount an actor token" without
	// enumerating agent names (08 §2.5).
	Role = "kube-agents/role"

	// Agent is the Agent CR name, and is what pairs the two halves: the broker Service selector,
	// the broker's NetworkPolicy, and every "show me both halves" query use it.
	Agent = "kube-agents/agent"
)

// The two legal values of Role. They are constants because they are compared against by admission
// policy: a typo'd role label is a pod that no hardening policy selects.
const (
	RoleReader = "reader"
	RoleActor  = "actor"
)

// maxLabelValue is the Kubernetes ceiling on a label value.
const maxLabelValue = 63

// digestChars is the length of the hex digest suffix, and prefixLen is what is left for the
// human-readable prefix once the digest and its separator are subtracted. Keeping a long prefix
// matters: an operator reading `kubectl get pods -L kube-agents/scope` should still be able to tell
// which scope a pod belongs to at a glance, and a value that is all digest is one nobody can read.
const (
	digestChars = 10
	prefixLen   = maxLabelValue - digestChars - 1
)

// For renders the five 08 §2.5 labels for one half of an Agent's pair. role must be RoleReader or
// RoleActor; any other value is passed through unchanged rather than corrected, because silently
// rewriting an unexpected role would hide the caller's bug behind a pod that admission then treats
// as something it is not.
//
// Callers merge this into whatever else they stamp (`app`, and the selector labels) rather than the
// reverse, so that a caller cannot accidentally shadow a 08 §2.5 key with its own.
//
// The tier is the EFFECTIVE tier, not the raw `spec.tier`. An Agent stored before the CRD default
// existed has an empty tier field, and stamping `kube-agents/tier: ""` on its pods would drop them
// out of every per-tier NetworkPolicy and out of `vap-agent-scope`'s selector -- the pod would run
// with no tier policy applied and nothing would report an error. agentindex.EffectiveTier is where
// the rest of the operator already resolves this, so it is where this resolves it too.
func For(agent *agentv1alpha1.Agent, role string) map[string]string {
	if agent == nil {
		return map[string]string{}
	}
	return map[string]string{
		Tier:   string(agentindex.EffectiveTier(agent)),
		Scope:  RenderScope(scope.Of(agent)),
		Parent: parentOf(agent),
		Role:   role,
		Agent:  agent.Name,
	}
}

// parentOf reads the lineage label. Platform agents have no parent and get the empty string, which
// is a legal label value and is meaningfully different from the label being absent: absent means
// "this controller did not stamp it", empty means "this agent is a root".
func parentOf(agent *agentv1alpha1.Agent) string {
	if agent.Spec.ParentRef == nil {
		return ""
	}
	return Sanitize(agent.Spec.ParentRef.Name)
}

// ScopeKey is the readable `<project>.<cluster>.<namespace>` join, with empty levels dropped so
// that a project-scoped platform agent renders `acme-prod` rather than `acme-prod..`.
//
// It is READABLE, not injective, and the difference is load-bearing: `{acme, prod.eu, payments}`
// and `{acme, prod, eu.payments}` are different scopes and join to the same string. That is why
// nothing hashes this value -- see canonical -- and why RenderScope only lets a join pass through
// once it has established that no level contains the separator.
func ScopeKey(s scope.Scope) string {
	parts := make([]string, 0, 3)
	for _, p := range []string{s.ProjectID, s.ClusterName, s.Namespace} {
		if p != "" {
			parts = append(parts, p)
		}
	}
	return strings.Join(parts, ".")
}

// canonical is the length-prefixed encoding the digest is taken over. Length-prefixed rather than
// delimiter-joined because a delimiter is only unambiguous if it cannot appear inside a level, and
// the entire reason a scope reaches the hashed path is that something about it was not ordinary.
// `%d:%s` per level is injective for any level content, including levels containing ':' or '.'.
func canonical(s scope.Scope) string {
	return fmt.Sprintf("%d:%s|%d:%s|%d:%s",
		len(s.ProjectID), s.ProjectID,
		len(s.ClusterName), s.ClusterName,
		len(s.Namespace), s.Namespace)
}

// RenderScope turns a scope into a label value that is legal, readable where it can be, and
// injective. See the package doc for why injectivity is the requirement and how the three rules
// deliver it.
func RenderScope(s scope.Scope) string {
	key := ScopeKey(s)
	// Rule 0: a scope with no levels at all renders to the empty string, which is a legal label
	// value. This is deliberate and not a fallthrough. Hashing it instead would produce
	// `scope-e3b0c44298` -- a value that LOOKS like a real scope, that an operator would go looking
	// for, and that would sit in `kubectl get pods -L kube-agents/scope` output as though the agent
	// were scoped to something. Empty says what is true: no scope was declared. It costs nothing in
	// injectivity, because no other input reaches it: rule 1 returns "" only when all three levels
	// are empty, and rules 2-3 always return at least `-` plus ten hex characters.
	if key == "" {
		return ""
	}
	// Rule 1: pass through only when the readable join is UNAMBIGUOUS as well as legal. Every
	// non-empty level must be a plain DNS-1123 label -- which forbids '.', so the join splits back
	// into exactly the levels it was built from -- and the scope must be well-formed, so that an
	// empty level appears only as a suffix. Without the well-formedness clause, the malformed
	// {cluster: c} and the ordinary {project: c} both join to "c".
	if s.IsWellFormed() && len(key) <= maxLabelValue && !looksHashed(key) && plainLevels(s) {
		return key
	}
	// Rules 2 and 3: the digest of the CANONICAL encoding carries the distinction. Hashing the
	// readable join instead would inherit the join's ambiguity, and hashing the TRUNCATION would
	// defeat the point entirely -- both mistakes read in a diff as "adds a hash suffix".
	sum := sha256.Sum256([]byte(canonical(s)))
	suffix := hex.EncodeToString(sum[:])[:digestChars]
	prefix := Sanitize(key)
	if len(prefix) > prefixLen {
		prefix = prefix[:prefixLen]
	}
	prefix = strings.TrimRight(prefix, "-_.")
	if prefix == "" {
		// A scope whose every level sanitizes away still has to render to something legal, and it
		// still has to be distinguishable from every other such scope -- which the digest is.
		return "scope-" + suffix
	}
	return prefix + "-" + suffix
}

// plainLevels reports whether every non-empty level is a DNS-1123 label: lowercase alphanumerics
// and '-', starting and ending alphanumeric. This is what project ids, cluster names and namespaces
// actually are, so the common case still renders readably; anything else takes the hashed path
// rather than being coerced into a value that might mean something else.
func plainLevels(s scope.Scope) bool {
	for _, p := range []string{s.ProjectID, s.ClusterName, s.Namespace} {
		if p == "" {
			continue
		}
		if len(validation.IsDNS1123Label(p)) > 0 {
			return false
		}
	}
	return true
}

// Sanitize coerces s into the character set a label value accepts, WITHOUT truncating or
// hash-suffixing. It is exported because the same coercion is what the journal applies to a
// non-scope value (an action's tier, a risk class) where the input is a closed enum and collision
// is impossible by construction. Anything derived from operator-supplied text must go through
// RenderScope instead.
func Sanitize(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
			b.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			// Lowercased rather than replaced, because `Payments` and `payments` are the same
			// namespace to a human and the digest is what keeps them apart when they are not.
			b.WriteRune(r + ('a' - 'A'))
		default:
			b.WriteRune('-')
		}
	}
	out := b.String()
	if len(out) > maxLabelValue {
		out = out[:maxLabelValue]
	}
	return strings.Trim(out, "-_.")
}

// looksHashed reports whether s already ends in something indistinguishable from renderValue's
// digest suffix. It is what keeps the pass-through set and the hashed set disjoint, and therefore
// what turns "these will not collide in practice" into "these cannot collide except on a digest
// collision".
func looksHashed(s string) bool {
	if len(s) < digestChars+1 || s[len(s)-digestChars-1] != '-' {
		return false
	}
	for _, r := range s[len(s)-digestChars:] {
		if !(r >= '0' && r <= '9') && !(r >= 'a' && r <= 'f') {
			return false
		}
	}
	return true
}
