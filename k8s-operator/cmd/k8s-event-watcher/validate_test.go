// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"strings"
	"testing"
)

// TestValidateScopeNamespace locks the D2 fail-closed rule (Phase 4 / P4-T3):
// a namespace-scoped tier (--owner=developer-team) MUST also pass
// --scope-namespace so its event informer is pinned server-side to one
// namespace. Cluster-wide tiers (platform, cluster-admin) run without it. The
// check runs at startup so a mis-rendered sidecar is rejected loudly rather
// than crash-looping (or, worse, ever observing events outside its tenant).
func TestValidateScopeNamespace(t *testing.T) {
	base := []string{
		"--daemon-url=http://127.0.0.1:8699",
		"--token-env=API_SERVER_KEY",
	}

	tests := []struct {
		name    string
		extra   []string
		wantErr string // substring; "" means expect success
	}{
		{
			name:    "developer-team without scope-namespace is rejected",
			extra:   []string{"--owner=developer-team"},
			wantErr: "--scope-namespace is required when --owner=developer-team",
		},
		{
			name:  "developer-team with scope-namespace is accepted",
			extra: []string{"--owner=developer-team", "--scope-namespace=team-a"},
		},
		{
			name:  "platform runs cluster-wide without scope-namespace",
			extra: []string{"--owner=platform"},
		},
		{
			name:  "cluster-admin runs cluster-wide without scope-namespace",
			extra: []string{"--owner=cluster-admin"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f, err := parseFlags(append(append([]string{}, base...), tt.extra...))
			if err != nil {
				t.Fatalf("parseFlags: unexpected error: %v", err)
			}
			err = f.validate()
			if tt.wantErr == "" {
				if err != nil {
					t.Fatalf("validate: unexpected error: %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("validate: expected error containing %q, got nil", tt.wantErr)
			}
			if !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("validate: error %q does not contain %q", err.Error(), tt.wantErr)
			}
		})
	}
}
