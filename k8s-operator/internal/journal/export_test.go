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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func exportableRecord(t *testing.T) *agentv1alpha1.ActionRecord {
	t.Helper()
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/my-project/cluster-a/team-x")
	ar.Name = RecordName(ar.Spec.ActionID)
	ar.Spec.Requester = agentv1alpha1.ActionRequester{Kind: "human", ID: "slack:U0A"}
	ar.Spec.AttributionUnverified = true
	ar.Spec.DryRun = true
	r, err := RetentionFor(agentv1alpha1.RiskElevated, submitted)
	if err != nil {
		t.Fatalf("RetentionFor: %v", err)
	}
	ar.Spec.Retention = r
	ar.Status.Phase = agentv1alpha1.PhaseDryRun
	ar.Status.Message = "classified and journaled, not executed"
	ar.Status.Contested = true
	ar.Status.UndoneBy = "01JZQ8X9K7M4N2P6R8T0V3W5ZZ"
	return ar
}

// The export is read months later by someone who has neither the cluster nor this code. Anything
// missing from it is unrecoverable by then, because the CR it referred to was garbage-collected on
// schedule -- which is the point of the post-export deletion predicate.
func TestEntryForCarriesTheWholeAttributionTriple(t *testing.T) {
	ar := exportableRecord(t)
	e := EntryFor(ar, submitted)

	if e.Event != "kube-agents.action" {
		t.Fatalf("Event = %q; a sink-side filter needs a stable discriminator", e.Event)
	}
	// All three, because they are not derivable from one another: the identity says which agent, the
	// SA says which credential actually wrote, and the requester says which human asked.
	if e.AgentIdentity != ar.Spec.AgentIdentity {
		t.Fatalf("AgentIdentity = %q", e.AgentIdentity)
	}
	if e.ActorServiceAccount != ar.Spec.ActorServiceAccount {
		t.Fatalf("ActorServiceAccount = %q", e.ActorServiceAccount)
	}
	if !strings.Contains(e.Requester, "slack:U0A") {
		t.Fatalf("Requester = %q, want it to name who asked", e.Requester)
	}
	// The marker on an unattributed action has to travel with it. An investigator reading an export
	// that silently dropped it would treat a guess as a fact (06 §8).
	if !e.AttributionUnverified {
		t.Fatal("attributionUnverified was dropped from the export")
	}

	for _, tc := range []struct{ name, got, want string }{
		{"actionId", e.ActionID, ar.Spec.ActionID},
		{"namespace", e.Namespace, ar.Namespace},
		{"name", e.Name, ar.Name},
		{"phase", e.Phase, string(ar.Status.Phase)},
		{"class", e.Class, string(ar.Spec.Classification.Class)},
		{"intent", e.Intent, ar.Spec.Intent},
		{"chainId", e.ChainID, ar.Spec.Trigger.ChainID},
		{"undoneBy", e.UndoneBy, ar.Status.UndoneBy},
		{"message", e.Message, ar.Status.Message},
	} {
		if tc.got != tc.want {
			t.Fatalf("%s = %q, want %q", tc.name, tc.got, tc.want)
		}
	}
	if !e.DryRun {
		t.Fatal("dryRun was dropped; the export would claim a change that never happened")
	}
	if !e.Contested {
		t.Fatal("contested was dropped; a change a human disagreed with would read as uncontroversial (06 §4.4)")
	}
	if len(e.Targets) != 1 || !strings.Contains(e.Targets[0], "api-gateway") {
		t.Fatalf("Targets = %v", e.Targets)
	}
	// The reader needs to know how long the working copy is still there to consult before this
	// export becomes the only copy.
	if !e.ExpiresAt.Equal(ar.Spec.Retention.ExpiresAt.Time) {
		t.Fatalf("ExpiresAt = %s, want %s", e.ExpiresAt, ar.Spec.Retention.ExpiresAt)
	}
	if !e.At.Equal(submitted) {
		t.Fatalf("At = %s, want the export instant %s", e.At, submitted)
	}
}

func TestEntryForOmitsUndoLinkageWhenThereIsNone(t *testing.T) {
	ar := exportableRecord(t)
	ar.Status.UndoneBy = ""
	ar.Spec.Trigger.UndoOf = ""
	e := EntryFor(ar, submitted)
	if e.UndoOf != "" || e.UndoneBy != "" {
		t.Fatalf("empty undo linkage was exported as UndoOf=%q UndoneBy=%q", e.UndoOf, e.UndoneBy)
	}
}

func TestWriterSinkEmitsOneJSONObjectPerLine(t *testing.T) {
	// On GKE this IS the Cloud Logging path: the node agent picks up container stdout and parses
	// structured JSON into jsonPayload. A line that is not valid JSON is dropped silently, so the
	// failure mode of getting this wrong is a missing audit record and no error anywhere.
	var buf bytes.Buffer
	s := NewWriterSink("stdout", &buf)
	ar := exportableRecord(t)

	for i := 0; i < 3; i++ {
		if err := s.Export(context.Background(), EntryFor(ar, submitted)); err != nil {
			t.Fatalf("Export: %v", err)
		}
	}
	lines := strings.Split(strings.TrimRight(buf.String(), "\n"), "\n")
	if len(lines) != 3 {
		t.Fatalf("got %d lines for 3 exports", len(lines))
	}
	for i, line := range lines {
		var e ExportEntry
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			t.Fatalf("line %d is not valid JSON, so the sink would drop it silently: %v\n%s", i, err, line)
		}
		if e.ActionID != ar.Spec.ActionID {
			t.Fatalf("line %d carries action id %q", i, e.ActionID)
		}
	}
	if s.Name() != "stdout" {
		t.Fatalf("Name = %q; status.exported.sink has to say where the evidence went", s.Name())
	}
}

func TestWriterSinkIsSafeUnderConcurrentExport(t *testing.T) {
	// The reconciler runs with concurrency > 1. Interleaved partial writes produce lines that are
	// not JSON -- and a sink drops those without complaining, so the corruption is invisible until
	// someone goes looking for evidence.
	var buf bytes.Buffer
	s := NewWriterSink("stdout", &buf)
	ar := exportableRecord(t)

	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := s.Export(context.Background(), EntryFor(ar, submitted)); err != nil {
				t.Errorf("Export: %v", err)
			}
		}()
	}
	wg.Wait()

	lines := strings.Split(strings.TrimRight(buf.String(), "\n"), "\n")
	if len(lines) != 64 {
		t.Fatalf("got %d lines for 64 concurrent exports", len(lines))
	}
	for i, line := range lines {
		var e ExportEntry
		if err := json.Unmarshal([]byte(line), &e); err != nil {
			t.Fatalf("line %d is torn: %v\n%s", i, err, line)
		}
	}
}

func TestMemorySinkCanBeMadeToFail(t *testing.T) {
	// Not a test of MemorySink so much as of the seam it provides. The post-export deletion
	// predicate is only observable against a sink that refuses -- with a sink that always succeeds,
	// "deletes only after export" and "deletes whenever" behave identically.
	m := &MemorySink{}
	ar := exportableRecord(t)
	if err := m.Export(context.Background(), EntryFor(ar, submitted)); err != nil {
		t.Fatalf("Export: %v", err)
	}
	if len(m.Entries()) != 1 {
		t.Fatalf("Entries() = %d", len(m.Entries()))
	}

	m.Err = errors.New("the bucket is retention-locked and full")
	if err := m.Export(context.Background(), EntryFor(ar, submitted)); err == nil {
		t.Fatal("a failing sink acknowledged an export")
	}
	if len(m.Entries()) != 1 {
		t.Fatal("a failed export was recorded anyway; a sink that acknowledges early makes the deletion predicate a no-op")
	}
}
