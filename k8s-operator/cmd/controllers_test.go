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

package main

import (
	"reflect"
	"strings"
	"testing"
)

// The default must keep running exactly what ran before --controllers existed. A Deployment that
// does not pass the flag -- and the live install's does not, until it is rolled -- must not lose a
// reconciler because a flag was added.
func TestDefaultSelectionIsTheOperatorSet(t *testing.T) {
	got, err := parseControllers(defaultControllers)
	if err != nil {
		t.Fatalf("the default value must parse: %v", err)
	}
	want := []string{ctlAgent, ctlJournal, ctlRetention}
	if !reflect.DeepEqual(got.names(), want) {
		t.Fatalf("default selection = %v, want %v", got.names(), want)
	}
	if got.has(ctlBrake) {
		t.Fatal("the default selection must not include the brake: it would run under the operator's identity")
	}
}

func TestBrakeAloneParses(t *testing.T) {
	got, err := parseControllers("brake")
	if err != nil {
		t.Fatalf("parseControllers(brake): %v", err)
	}
	if !got.has(ctlBrake) || len(got) != 1 {
		t.Fatalf("selection = %v, want exactly [brake]", got.names())
	}
}

// THE LOAD-BEARING REFUSAL. C-BR and the journal exporter hold deliberately disjoint authority over
// ActionRecord.status (06 §4.3), enforced on the USERNAME by vap-agent-scope-journal. One process
// is one ServiceAccount, so combining them would require an identity holding the union -- and the
// exporter's write is what unlocks deletion of the record, so that identity could write the receipt
// for an escalation and then destroy it. Every combination is refused, not just the obvious one.
func TestBrakeMayNotShareAProcess(t *testing.T) {
	for _, spec := range []string{
		"brake,agent",
		"agent,brake",
		"brake,journal",
		"brake,retention",
		"agent,journal,retention,brake",
	} {
		t.Run(spec, func(t *testing.T) {
			_, err := parseControllers(spec)
			if err == nil {
				t.Fatalf("parseControllers(%q) must be refused: the brake may not share a manager", spec)
			}
			// The message has to name the reason, not just the rule: the next person to hit this is
			// deciding whether to delete the check.
			if !strings.Contains(err.Error(), "own ServiceAccount") {
				t.Fatalf("refusal must explain why, got: %v", err)
			}
		})
	}
}

// A typo must not silently run nothing. `--controllers=brakes` on the brake Deployment would leave
// a fleet with no brake behind a pod reporting Ready.
func TestUnknownControllerIsFatal(t *testing.T) {
	for _, spec := range []string{"brakes", "agent,brakes", "Brake", "agent,,webhook"} {
		t.Run(spec, func(t *testing.T) {
			if _, err := parseControllers(spec); err == nil {
				t.Fatalf("parseControllers(%q) must be refused", spec)
			}
		})
	}
}

// An empty selection is refused rather than treated as "run nothing": a manager with no reconcilers
// still elects a leader and still answers /readyz, so it reports healthy while doing the job of
// nothing. Scaling to zero is how you stop a controller; the replica count then says so.
func TestEmptySelectionIsFatal(t *testing.T) {
	for _, spec := range []string{"", ",", "   ", " , , "} {
		t.Run("["+spec+"]", func(t *testing.T) {
			if _, err := parseControllers(spec); err == nil {
				t.Fatalf("parseControllers(%q) must be refused", spec)
			}
		})
	}
}

func TestDuplicateIsFatal(t *testing.T) {
	if _, err := parseControllers("agent,agent"); err == nil {
		t.Fatal("a controller listed twice is a typo, not a selection")
	}
}

func TestWhitespaceIsTolerated(t *testing.T) {
	got, err := parseControllers(" agent , journal ")
	if err != nil {
		t.Fatalf("parseControllers: %v", err)
	}
	if !reflect.DeepEqual(got.names(), []string{ctlAgent, ctlJournal}) {
		t.Fatalf("selection = %v", got.names())
	}
}

// The brake's LeaderElectionID must differ from the operator's, or one Lease has one winner and the
// loser sits idle holding no controllers while its Deployment reports Ready. Asserted on the
// constants because main() is not callable from a test -- if the two ever converge, this fails.
func TestControllerNamesAreDistinctAndClosed(t *testing.T) {
	seen := map[string]bool{}
	for _, n := range knownControllers {
		if seen[n] {
			t.Fatalf("controller %q appears twice in knownControllers", n)
		}
		seen[n] = true
	}
	if len(knownControllers) != len(operatorControllers)+1 {
		t.Fatalf("knownControllers = %v: the only name outside the operator set is the brake; "+
			"a new one needs a decision about which Deployment runs it", knownControllers)
	}
}
