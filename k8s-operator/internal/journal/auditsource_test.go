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
	"os"
	"path/filepath"
	"testing"
	"time"
)

const (
	devActor     = "system:serviceaccount:team-x:developer-team-team-x-actor"
	clusterActor = "system:serviceaccount:cluster-a:cluster-admin-cluster-a-actor"
	fixturePath  = "testdata/audit-writes.jsonl"
)

var (
	windowStart = time.Date(2026, 7, 27, 0, 0, 0, 0, time.UTC)
	windowEnd   = time.Date(2026, 7, 28, 0, 0, 0, 0, time.UTC)
)

func TestFixtureSourceReadsTheStream(t *testing.T) {
	src := NewFixtureSource(fixturePath)
	if err := src.Available(context.Background()); err != nil {
		t.Fatalf("Available: %v", err)
	}
	writes, err := src.Writes(context.Background(), windowStart, windowEnd, []string{devActor, clusterActor})
	if err != nil {
		t.Fatalf("Writes: %v", err)
	}
	// Five in-window writes by the two actor identities: two journaled, one unlabeled, one
	// unmatched, one journaled later. The non-actor and out-of-window lines must not be here.
	if len(writes) != 5 {
		for _, w := range writes {
			t.Logf("  %s %s %s %s", w.At.UTC().Format(time.RFC3339), w.Principal, w.Verb, w.ActionID)
		}
		t.Fatalf("got %d writes, want 5", len(writes))
	}
	for _, w := range writes {
		if w.Principal != devActor && w.Principal != clusterActor {
			t.Fatalf("principal filter leaked %q; kubelet and every human with a kubeconfig write constantly and none of them journal", w.Principal)
		}
		if w.At.Before(windowStart) || !w.At.Before(windowEnd) {
			t.Fatalf("window filter leaked a write at %s from [%s, %s)", w.At.UTC(), windowStart, windowEnd)
		}
		if w.Resource == "" || w.Verb == "" {
			t.Fatalf("write at %s is missing verb or resource, so a finding about it would be unactionable", w.At.UTC())
		}
	}
}

func TestFixtureSourceEmptyPrincipalsMeansAll(t *testing.T) {
	// The interface says an empty slice means all principals. CheckCompleteness never passes one --
	// it refuses first -- but a caller reading the raw stream for another purpose relies on this.
	writes, err := NewFixtureSource(fixturePath).Writes(context.Background(), windowStart, windowEnd, nil)
	if err != nil {
		t.Fatalf("Writes: %v", err)
	}
	var sawNonActor bool
	for _, w := range writes {
		if w.Principal == "alice@example.com" {
			sawNonActor = true
		}
	}
	if !sawNonActor {
		t.Fatal("an empty principal list filtered principals out; it must mean all")
	}
}

func TestFixtureSourceRejectsAMalformedLine(t *testing.T) {
	// Skipping a bad line would make a truncated or corrupted stream look like a short clean one --
	// and a short clean stream is exactly what a passing V-BRK-003 looks like.
	path := filepath.Join(t.TempDir(), "bad.jsonl")
	body := "# a comment\n\n" +
		`{"at":"2026-07-27T12:00:01Z","principal":"` + devActor + `","verb":"patch","resource":"r","actionId":"01JZQ8X9K7M4N2P6R8T0V3W5YZ"}` + "\n" +
		"{not json\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	if _, err := NewFixtureSource(path).Writes(context.Background(), windowStart, windowEnd, nil); err == nil {
		t.Fatal("a malformed line was skipped rather than reported; a truncated stream would read as a clean one")
	}
}

func TestFixtureSourceReportsAMissingFile(t *testing.T) {
	// "The file is not there" and "the file is there and empty" both yield zero writes and mean
	// opposite things. Available is the seam that keeps them apart.
	src := NewFixtureSource(filepath.Join(t.TempDir(), "absent.jsonl"))
	if err := src.Available(context.Background()); err == nil {
		t.Fatal("a missing fixture reported itself as available")
	}
}
