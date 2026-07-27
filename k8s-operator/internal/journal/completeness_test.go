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
	"errors"
	"strings"
	"testing"
	"time"
)

// The action ids the fixture's journaled writes carry. The two negative-control ids are absent on
// purpose -- that absence IS the fixture.
var journaledIDs = SetLookup{
	"01JZQ8X9K7M4N2P6R8T0V3W5YZ": true,
	"01JZQ8X9K7M4N2P6R8T0V3W5ZZ": true,
}

var actorPrincipals = []string{devActor, clusterActor}

// A green V-BRK-003 on a healthy cluster is vacuous: no unjournaled writes exist, so a scan that
// does nothing produces the same result as a scan that works. This is the run that distinguishes
// them.
func TestCheckCompletenessFindsBothKindsOfUnjournaledWrite(t *testing.T) {
	res, err := CheckCompleteness(context.Background(), NewFixtureSource(fixturePath), journaledIDs, actorPrincipals, windowStart, windowEnd)
	if err != nil {
		t.Fatalf("CheckCompleteness: %v", err)
	}
	if res.Scanned != 5 {
		t.Fatalf("Scanned = %d, want 5", res.Scanned)
	}
	if res.Passed() {
		t.Fatal("the negative-control fixture passed; the check does not detect an unjournaled write, and its green verdict on a real cluster means nothing")
	}
	if len(res.Findings) != 2 {
		for _, f := range res.Findings {
			t.Logf("  %s", f)
		}
		t.Fatalf("got %d findings, want 2", len(res.Findings))
	}

	byKind := map[FindingKind]Finding{}
	for _, f := range res.Findings {
		byKind[f.Kind] = f
	}
	unlabeled, ok := byKind[FindingUnlabeled]
	if !ok {
		t.Fatal("no `unlabeled` finding: a write from an actor identity carrying no action id at all went unreported, and that is an admission-policy failure (05 §1.1)")
	}
	if unlabeled.Write.Verb != "delete" || unlabeled.Write.ActionID != "" {
		t.Fatalf("unlabeled finding points at the wrong write: %+v", unlabeled.Write)
	}
	unmatched, ok := byKind[FindingUnmatched]
	if !ok {
		t.Fatal("no `unmatched` finding: a write whose action id names no record went unreported, and that is a broker failure")
	}
	if unmatched.Write.ActionID != "01JZQ8X9K7M4N2P6R8T0V3WAAA" {
		t.Fatalf("unmatched finding points at the wrong write: %+v", unmatched.Write)
	}

	// The two are fixed in different places, so a finding that does not say which one it is sends
	// the reader to the wrong file.
	if !strings.Contains(unlabeled.String(), ActionIDLabel) {
		t.Fatalf("the unlabeled finding does not name the missing label: %s", unlabeled)
	}
	if !strings.Contains(unmatched.String(), "not journaled") {
		t.Fatalf("the unmatched finding does not say the write was unjournaled: %s", unmatched)
	}
	if !strings.Contains(res.Summary(), "FAIL") || !strings.Contains(res.Summary(), NewFixtureSource(fixturePath).Name()) {
		t.Fatalf("the summary does not carry the verdict and its evidence: %s", res.Summary())
	}
}

func TestCheckCompletenessPassesWhenEveryWriteIsJournaled(t *testing.T) {
	// The positive direction still has to be checked, or a scan that reported everything as a
	// finding would satisfy the test above.
	all := SetLookup{
		"01JZQ8X9K7M4N2P6R8T0V3W5YZ": true,
		"01JZQ8X9K7M4N2P6R8T0V3W5ZZ": true,
		"01JZQ8X9K7M4N2P6R8T0V3WAAA": true,
	}
	// Only the cluster-admin actor: its two writes both carry ids that now resolve.
	res, err := CheckCompleteness(context.Background(), NewFixtureSource(fixturePath), all, []string{clusterActor}, windowStart, windowEnd)
	if err != nil {
		t.Fatalf("CheckCompleteness: %v", err)
	}
	if !res.Passed() {
		for _, f := range res.Findings {
			t.Logf("  %s", f)
		}
		t.Fatal("a fully journaled window produced findings")
	}
	if res.Scanned != 2 {
		t.Fatalf("Scanned = %d, want 2", res.Scanned)
	}
}

func TestCheckCompletenessRefusesAnEmptyPrincipalList(t *testing.T) {
	// Scanning every principal sweeps in kubelet, the controller-manager and every human with a
	// kubeconfig. The resulting wall of findings is indistinguishable from a broken check, and the
	// realistic outcome is a blanket waiver.
	_, err := CheckCompleteness(context.Background(), NewFixtureSource(fixturePath), journaledIDs, nil, windowStart, windowEnd)
	if err == nil {
		t.Fatal("CheckCompleteness ran against all principals instead of refusing")
	}
}

// deadSource is a stream that cannot be read -- the shape of a cluster with Data Access audit logs
// switched off.
type deadSource struct{}

func (deadSource) Name() string { return "dead" }
func (deadSource) Available(context.Context) error {
	return errors.New("Data Access audit logs are not enabled")
}
func (deadSource) Writes(context.Context, time.Time, time.Time, []string) ([]AuditWrite, error) {
	return nil, nil
}

func TestCheckCompletenessTreatsAnUnavailableSourceAsAnError(t *testing.T) {
	// This is the whole of 09 §9.6 in one assertion. An unreadable stream yields zero writes, zero
	// writes yields zero findings, and zero findings reads exactly like a clean cluster. Returning
	// an error is what forces the caller to record `deferred` with a named blocker rather than
	// `pass` -- and V-BRK-003 is BLOCKING-ALWAYS, so the difference decides whether a release ships.
	res, err := CheckCompleteness(context.Background(), deadSource{}, journaledIDs, actorPrincipals, windowStart, windowEnd)
	if err == nil {
		t.Fatal("an unreadable audit stream produced a clean result instead of an error")
	}
	if res.Passed() && res.Scanned == 0 && err == nil {
		t.Fatal("silence was mistaken for cleanliness")
	}
	if !strings.Contains(err.Error(), "dead") {
		t.Fatalf("the error does not name the source, so the verdict cannot say what evidence it rests on: %v", err)
	}
}

func TestCheckCompletenessPropagatesALookupFailure(t *testing.T) {
	// "The lookup failed" must never collapse into "no record exists": the first is a broken check
	// and the second is a critical finding, and reporting the second for the first would send
	// someone hunting a broker bug that is not there.
	boom := LookupFunc(func(context.Context, string) (bool, error) {
		return false, errors.New("the API server is unreachable")
	})
	if _, err := CheckCompleteness(context.Background(), NewFixtureSource(fixturePath), boom, actorPrincipals, windowStart, windowEnd); err == nil {
		t.Fatal("a failed record lookup was reported as an unjournaled write")
	}
}

func TestCheckCompletenessMemoizesLookups(t *testing.T) {
	// One action commonly writes several objects. Without memoization a fan-out action produces one
	// API call per written object, and the scan's cost grows with the busiest action in the window.
	var calls int
	counting := LookupFunc(func(_ context.Context, id string) (bool, error) {
		calls++
		return journaledIDs[id], nil
	})
	if _, err := CheckCompleteness(context.Background(), NewFixtureSource(fixturePath), counting, actorPrincipals, windowStart, windowEnd); err != nil {
		t.Fatalf("CheckCompleteness: %v", err)
	}
	// Three distinct ids across four labelled writes.
	if calls != 3 {
		t.Fatalf("lookup called %d times for 3 distinct action ids", calls)
	}
}

func TestCheckCompletenessSortsFindingsByTime(t *testing.T) {
	res, err := CheckCompleteness(context.Background(), NewFixtureSource(fixturePath), journaledIDs, actorPrincipals, windowStart, windowEnd)
	if err != nil {
		t.Fatalf("CheckCompleteness: %v", err)
	}
	for i := 1; i < len(res.Findings); i++ {
		if res.Findings[i].Write.At.Before(res.Findings[i-1].Write.At) {
			t.Fatal("findings are not in time order; an investigator reading them cannot follow the sequence of events")
		}
	}
}
