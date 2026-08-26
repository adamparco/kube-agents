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

package execute

import (
	"fmt"
	"strings"
)

// The field manager of 03 §4.1 step 9: `kube-agents/<tier>/<scope>`.
//
// It is a security-relevant identifier rather than a label. Server-side apply records it in
// `metadata.managedFields`, and two properties are built on that record:
//
//   - `contested` (03 §6, 04 §4.2) detects a NON-agent manager touching a field an agent owns. That
//     comparison is a string comparison against this prefix, so a manager string that drifts by one
//     character reads as a foreign manager and every agent-owned object becomes contested -- or,
//     depending on which side drifted, no object ever does.
//   - Ownership between agents is per-(tier, scope). Two agents sharing a manager string share
//     ownership of the same fields, which is the one way a scope boundary can be crossed without any
//     RBAC being wrong.
//
// So the string is produced here, once, from the same `<tier>/<scope>` key the journal indexes on
// (`broker.Identity.AgentIdentity`), and never assembled at a call site. LSN-031 is the lesson:
// a decision the codebase has already made, re-made by hand downstream, fails toward silence.
const (
	// FieldManagerPrefix is the only prefix an agent-owned field carries.
	FieldManagerPrefix = "kube-agents/"

	// MaxFieldManagerLength is the API server's limit (`metav1.ValidateFieldManager`). Exceeding it
	// is a 400 from the apply, which would surface as a mysterious per-agent execution failure for
	// long scopes rather than as the naming problem it is.
	MaxFieldManagerLength = 128
)

// FieldManager returns the server-side-apply manager for an agent identity.
//
// The argument is the `<tier>/<scope>` key, i.e. exactly what `Identity.AgentIdentity()` returns --
// including its scope-less form for a tier that has no scope, where the manager is
// `kube-agents/<tier>`. That case is real (a platform agent is project-scoped and its scope segment
// is empty), and inventing a placeholder segment for it would produce a manager string that matches
// no agent and no human.
//
// It returns an error rather than a best-effort string for every malformed input. A field manager is
// not a display name: the apply succeeds with whatever it is given, so a wrong one is invisible
// until something downstream compares it, which is exactly when the comparison matters.
func FieldManager(agentIdentity string) (string, error) {
	switch {
	case agentIdentity == "":
		return "", fmt.Errorf("field manager: the agent identity is empty; the manager must name the acting agent")
	case strings.HasPrefix(agentIdentity, FieldManagerPrefix):
		// Double-prefixing is the mistake a caller makes when it has already seen a manager string
		// and passes it back in. `kube-agents/kube-agents/platform` is a valid manager the API
		// server will happily record, and it belongs to nobody.
		return "", fmt.Errorf("field manager: the agent identity %q already carries the %q prefix; pass the <tier>/<scope> key, not a manager string", agentIdentity, FieldManagerPrefix)
	case strings.ContainsAny(agentIdentity, " \t\n\r"):
		return "", fmt.Errorf("field manager: the agent identity %q contains whitespace", agentIdentity)
	case strings.Contains(agentIdentity, "*"):
		// A wildcard here would be a manager string that reads, to a human scanning managedFields,
		// as an agent with authority over everything.
		return "", fmt.Errorf("field manager: the agent identity %q contains a wildcard", agentIdentity)
	}

	for _, seg := range strings.Split(agentIdentity, "/") {
		if seg == "" {
			return "", fmt.Errorf("field manager: the agent identity %q has an empty segment; expected <tier> or <tier>/<scope>", agentIdentity)
		}
	}
	if n := strings.Count(agentIdentity, "/"); n > 1 {
		return "", fmt.Errorf("field manager: the agent identity %q has %d separators; expected <tier> or <tier>/<scope>", agentIdentity, n)
	}

	manager := FieldManagerPrefix + agentIdentity
	if len(manager) > MaxFieldManagerLength {
		return "", fmt.Errorf("field manager: %q is %d bytes, over the API server's %d-byte limit", manager, len(manager), MaxFieldManagerLength)
	}
	return manager, nil
}

// IsAgentManager reports whether a manager string in `metadata.managedFields` belongs to some agent.
//
// The negation is what `contested` is built on, so this is deliberately the ONLY place the prefix is
// tested. A caller writing `strings.HasPrefix(m, "kube-agents/")` inline is a second definition of
// "is this ours", and the two would agree until one of them was updated.
func IsAgentManager(manager string) bool {
	return strings.HasPrefix(manager, FieldManagerPrefix) && len(manager) > len(FieldManagerPrefix)
}

// AgentIdentityOfManager recovers the `<tier>/<scope>` key from a manager string, or "" if the
// string is not an agent manager. The inverse of FieldManager, and tested as a round trip: an
// inverse that is nearly right is how an ownership comparison starts matching the wrong agent.
func AgentIdentityOfManager(manager string) string {
	if !IsAgentManager(manager) {
		return ""
	}
	return strings.TrimPrefix(manager, FieldManagerPrefix)
}
