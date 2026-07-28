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
	"strings"
	"testing"
)

func TestFieldManagerExactString(t *testing.T) {
	// V-BRK-006 says the manager is EXACTLY `kube-agents/<tier>/<scope>`. Asserted as a literal
	// rather than as a re-composition of the same constants, because a test built from the code's
	// own pieces agrees with the code by construction, including when both are wrong.
	got, err := FieldManager("cluster-admin/prod-usc1")
	if err != nil {
		t.Fatalf("FieldManager: %v", err)
	}
	if got != "kube-agents/cluster-admin/prod-usc1" {
		t.Fatalf("field manager = %q, want %q", got, "kube-agents/cluster-admin/prod-usc1")
	}
}

func TestFieldManagerScopeless(t *testing.T) {
	got, err := FieldManager("platform")
	if err != nil {
		t.Fatalf("FieldManager: %v", err)
	}
	if got != "kube-agents/platform" {
		t.Fatalf("field manager = %q, want %q", got, "kube-agents/platform")
	}
}

func TestFieldManagerRejects(t *testing.T) {
	cases := []struct {
		name     string
		identity string
	}{
		{"empty", ""},
		{"already prefixed", "kube-agents/platform"},
		{"whitespace", "cluster-admin/prod usc1"},
		{"newline", "cluster-admin/prod\n"},
		{"wildcard", "cluster-admin/*"},
		{"empty tier", "/prod"},
		{"empty scope", "cluster-admin/"},
		{"three segments", "developer-team/prod/team-a"},
		{"over the length limit", "cluster-admin/" + strings.Repeat("x", 130)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := FieldManager(tc.identity)
			if err == nil {
				t.Fatalf("FieldManager(%q) = %q, want an error", tc.identity, got)
			}
			if got != "" {
				t.Fatalf("FieldManager(%q) returned %q alongside its error; a rejected identity must not yield a usable manager", tc.identity, got)
			}
		})
	}
}

func TestIsAgentManager(t *testing.T) {
	cases := map[string]bool{
		"kube-agents/platform":              true,
		"kube-agents/cluster-admin/prod":    true,
		"kube-agents/":                      false, // the prefix alone names no agent
		"kubectl-client-side-apply":         false,
		"kube-controller-manager":           false,
		"kube-agents":                       false,
		"not-kube-agents/cluster-admin":     false,
		"KUBE-AGENTS/platform":              false, // manager strings are case-sensitive
		" kube-agents/platform":             false,
		"kube-agents-shadow/cluster-admin":  false, // the near-miss a substring test would accept
		"kube-agents/developer-team/team-a": true,
	}
	for manager, want := range cases {
		if got := IsAgentManager(manager); got != want {
			t.Errorf("IsAgentManager(%q) = %v, want %v", manager, got, want)
		}
	}
}

func TestAgentIdentityRoundTrip(t *testing.T) {
	// The inverse must be exact for every identity FieldManager accepts, because ownership
	// comparisons run through it: an inverse that is nearly right attributes a field to the wrong
	// agent, which reads in an audit as the wrong agent having made the change.
	for _, identity := range []string{
		"platform",
		"cluster-admin/prod-usc1",
		"developer-team/team-a",
	} {
		manager, err := FieldManager(identity)
		if err != nil {
			t.Fatalf("FieldManager(%q): %v", identity, err)
		}
		if !IsAgentManager(manager) {
			t.Fatalf("IsAgentManager(%q) = false for a manager this package produced", manager)
		}
		if got := AgentIdentityOfManager(manager); got != identity {
			t.Fatalf("round trip: %q -> %q -> %q", identity, manager, got)
		}
	}
}

func TestAgentIdentityOfForeignManager(t *testing.T) {
	for _, manager := range []string{"kubectl", "kube-controller-manager", "", "kube-agents/"} {
		if got := AgentIdentityOfManager(manager); got != "" {
			t.Errorf("AgentIdentityOfManager(%q) = %q, want \"\"", manager, got)
		}
	}
}
