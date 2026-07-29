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
	"fmt"
	"sort"
	"strings"
)

// The closed set of controllers this binary can run. One image, one binary, and which reconcilers
// wake up is a flag — so the brake controller is not a second program to build, sign and roll.
const (
	ctlAgent     = "agent"
	ctlJournal   = "journal"
	ctlRetention = "retention"
	ctlBrake     = "brake"
)

// operatorControllers is the historical default: everything that ran before `--controllers`
// existed. Spelled out rather than "all minus brake" so that adding a controller to the binary is
// a deliberate decision about which Deployment gets it, not something that happens by omission.
var operatorControllers = []string{ctlAgent, ctlJournal, ctlRetention}

// defaultControllers is what a process runs when nobody says. It is the operator set, which is
// what makes `--controllers` a strictly additive change: an existing Deployment that does not pass
// the flag runs exactly what it ran yesterday.
var defaultControllers = strings.Join(operatorControllers, ",")

var knownControllers = append(append([]string{}, operatorControllers...), ctlBrake)

// controllerSet is the parsed `--controllers` value.
type controllerSet map[string]bool

func (s controllerSet) has(name string) bool { return s[name] }

// names returns the selection in a stable order, for logging.
func (s controllerSet) names() []string {
	out := make([]string, 0, len(s))
	for n := range s {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// parseControllers turns the flag value into a selection, or refuses.
//
// # Why `brake` may not be combined with anything
//
// A process runs as exactly one ServiceAccount, so "which controllers are in this binary" and
// "which identity are they authorised as" are the same question. 06 §4.3 gives C-BR and the
// journal exporter deliberately disjoint authority over `ActionRecord.status` — the exporter may
// write `exported` and nothing else, C-BR may write the fulfilment half of `escalation` and
// nothing else — and `vap-agent-scope-journal` enforces both as admission decisions on the
// username. Running them in one process would mean one ServiceAccount that needs the union, and
// the union is the thing the split exists to prevent: the exporter's write is what unlocks
// deletion of the record, so an identity holding both could write the receipt for an escalation
// and then destroy the evidence of it.
//
// Refusing the combination here, at parse time, is what stops that from being one line of
// kustomize away. The alternative — a comment in the Deployment asking the next person not to —
// is not a control.
//
// An unknown name is fatal for the ordinary reason: `--controllers=brakes` silently running
// nothing is a fleet with no brake and a Deployment that looks healthy.
func parseControllers(spec string) (controllerSet, error) {
	fields := strings.Split(spec, ",")
	set := controllerSet{}
	for _, raw := range fields {
		name := strings.TrimSpace(raw)
		if name == "" {
			continue
		}
		if !knownName(name) {
			return nil, fmt.Errorf("unknown controller %q: known controllers are %s",
				name, strings.Join(knownControllers, ", "))
		}
		if set[name] {
			return nil, fmt.Errorf("controller %q listed twice", name)
		}
		set[name] = true
	}

	if len(set) == 0 {
		// Not a no-op worth allowing. A manager with no reconcilers still elects a leader, still
		// serves /readyz, and still reports Ready — a pod that looks like it is doing the job and
		// is not. If the intent is to stop a controller, scale the Deployment to zero, where the
		// replica count says so.
		return nil, fmt.Errorf("--controllers selected nothing: name at least one of %s",
			strings.Join(knownControllers, ", "))
	}

	if set[ctlBrake] && len(set) > 1 {
		others := set.names()
		return nil, fmt.Errorf(
			"--controllers=%s combines %q with %v: C-BR runs under its own ServiceAccount and a "+
				"process runs as one identity, so the brake may not share a manager with any other "+
				"controller (06 §4.3, vap-agent-scope-journal validations 5 and 6)",
			spec, ctlBrake, others)
	}

	return set, nil
}

func knownName(name string) bool {
	for _, k := range knownControllers {
		if k == name {
			return true
		}
	}
	return false
}
