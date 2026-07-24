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

package router

import (
	"regexp"
	"strings"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
)

// tierPrefix maps a handle prefix to its tier (06 §2b handle table). ORDER MATTERS: the table is
// scanned top-to-bottom and the FIRST matching prefix wins, so the most-specific canonical prefix must
// precede its shorter alias — otherwise `cluster-admin-<x>` would mis-parse as the `cluster-` alias
// with leaf `admin-<x>`. Canonical forms are listed before aliases for exactly this reason.
var tierPrefixes = []struct {
	prefix string
	tier   agentv1alpha1.AgentTier
}{
	{"platform-", agentv1alpha1.TierPlatform},
	{"cluster-admin-", agentv1alpha1.TierClusterAdmin}, // canonical — must precede "cluster-"
	{"cluster-", agentv1alpha1.TierClusterAdmin},       // short alias
	{"developer-team-", agentv1alpha1.TierDeveloperTeam},
	{"devteam-", agentv1alpha1.TierDeveloperTeam}, // short alias
}

// rfc1123Label matches a single DNS-1123 label (the shape of every GKE project/cluster/namespace
// leaf). A handle whose leaf is not a valid label is refused, never coerced — the router does not guess.
var rfc1123Label = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)

// parseHandleToken turns a bare handle token (no leading `@` or `/`) into a Handle. Matching is
// case-insensitive on the tier prefix; the leaf is lower-cased and validated as an RFC1123 label.
// Returns ErrUnknownTier if no tier prefix matches and ErrMalformedHandle if the leaf is empty/invalid.
func parseHandleToken(tok string) (Handle, error) {
	tok = strings.ToLower(strings.TrimSpace(tok))
	for _, tp := range tierPrefixes {
		if strings.HasPrefix(tok, tp.prefix) {
			leaf := tok[len(tp.prefix):]
			if leaf == "" || len(leaf) > 63 || !rfc1123Label.MatchString(leaf) {
				return Handle{}, ErrMalformedHandle
			}
			return Handle{Tier: tp.tier, Leaf: leaf}, nil
		}
	}
	return Handle{}, ErrUnknownTier
}

// Canonical returns the canonical @handle form for audit attribution (06 §2b). It always uses the
// canonical (not the short-alias) prefix so two spellings of the same target log identically.
func (h Handle) Canonical() string {
	switch h.Tier {
	case agentv1alpha1.TierPlatform:
		return "@platform-" + h.Leaf
	case agentv1alpha1.TierClusterAdmin:
		return "@cluster-admin-" + h.Leaf
	case agentv1alpha1.TierDeveloperTeam:
		return "@developer-team-" + h.Leaf
	default:
		return "@" + string(h.Tier) + "-" + h.Leaf
	}
}

// RouteKey turns a parsed handle into the agentindex routing/cardinality key so a resolved handle and
// an indexed Agent CR produce the SAME key — the no-drift guarantee (both go through agentindex). It
// serves the two tiers whose full scope key is derivable from (handle leaf + router project context):
//
//   - platform:       the leaf IS the project; projectID is ignored.
//   - cluster-admin:  key = (cluster-admin, projectID, leaf); projectID required (ErrMissingProjectContext).
//
// A developer-team handle carries only its namespace leaf and cannot name a cluster, so its full key
// exists only on a live CR — it is resolved via Index.LookupHandle (the byTierLeaf secondary index),
// NOT here. RouteKey therefore has no developer-team branch; call it only through Index.LookupHandle,
// which routes each tier by the correct mechanism.
func (h Handle) RouteKey(projectID string) (string, error) {
	switch h.Tier {
	case agentv1alpha1.TierPlatform:
		return agentindex.Identity(agentv1alpha1.TierPlatform, h.Leaf, "", ""), nil
	case agentv1alpha1.TierClusterAdmin:
		if projectID == "" {
			return "", ErrMissingProjectContext
		}
		return agentindex.Identity(agentv1alpha1.TierClusterAdmin, projectID, h.Leaf, ""), nil
	default:
		return "", ErrUnknownTier
	}
}

// HandleForAgent derives the canonical Handle of an Agent CR from its (tier, scope) — the reverse of
// parsing chat text. The index uses it for audit strings; deriving it from the same tier+leaf the CR
// carries keeps the handle table and the CRs from drifting. Leaf is taken from the per-tier scope field.
func HandleForAgent(a *agentv1alpha1.Agent) Handle {
	tier := agentindex.EffectiveTier(a)
	var leaf string
	if s := a.Spec.Scope; s != nil {
		switch tier {
		case agentv1alpha1.TierPlatform:
			leaf = s.ProjectID
		case agentv1alpha1.TierClusterAdmin:
			leaf = s.ClusterName
		case agentv1alpha1.TierDeveloperTeam:
			leaf = s.Namespace
		}
	}
	return Handle{Tier: tier, Leaf: leaf}
}
