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
	"fmt"
	"sort"
	"time"
)

// V-BRK-003: "every audit-log write by an actor identity has a matching ActionRecord."
//
// This is the only check in the suite that reads something OTHER than the journal to judge the
// journal. Every other broker check asks the journal what happened, so a broker that wrote without
// journaling would satisfy all of them -- the missing record is invisible from inside. Here the
// API server's own audit stream is the second witness.
//
// Two distinct findings come out of it, and collapsing them would be a mistake:
//
//	unlabeled  -- an actor identity wrote with no `kube-agents/action-id` at all. Admission is
//	              supposed to make this impossible (05 §1.1), so it means the policy is missing,
//	              or something authenticated as an actor SA outside the broker.
//	unmatched  -- the write carried an action id for which no ActionRecord exists. The broker
//	              journaled after writing, or not at all, or the record was deleted early.
//
// The first is a policy failure and the second is a broker failure. They are found by the same scan
// and fixed in different places.

// FindingKind distinguishes the two ways a write can be unjournaled.
type FindingKind string

const (
	// FindingUnlabeled is a write by an actor identity carrying no action id.
	FindingUnlabeled FindingKind = "unlabeled"
	// FindingUnmatched is a write whose action id names no ActionRecord.
	FindingUnmatched FindingKind = "unmatched"
)

// Finding is one unjournaled write.
type Finding struct {
	Kind      FindingKind
	Write     AuditWrite
	Namespace string
}

// String renders a finding for a check's output. It names the principal, the object, and the reason,
// because a finding a human cannot act on is a finding that gets waived.
func (f Finding) String() string {
	switch f.Kind {
	case FindingUnlabeled:
		return fmt.Sprintf(
			"%s: actor %q %s %s at %s with no %s label -- admission should have rejected this write (05 §1.1)",
			f.Kind, f.Write.Principal, f.Write.Verb, f.Write.Resource, f.Write.At.UTC().Format(time.RFC3339), ActionIDLabel)
	default:
		return fmt.Sprintf(
			"%s: actor %q %s %s at %s carrying action id %s, for which no ActionRecord exists -- the write was not journaled (V-BRK-003)",
			f.Kind, f.Write.Principal, f.Write.Verb, f.Write.Resource, f.Write.At.UTC().Format(time.RFC3339), f.Write.ActionID)
	}
}

// RecordLookup answers "does an ActionRecord exist for this action id, anywhere?". It is an
// interface so the completeness scan can run against a live cluster, a fixture set, or an exported
// stream without three copies of the matching logic.
type RecordLookup interface {
	// Exists reports whether a record with this action id exists. An error means the lookup could
	// not be performed; the caller must treat that as a failed check, never as "no record".
	Exists(ctx context.Context, actionID string) (bool, error)
}

// LookupFunc adapts a function to RecordLookup.
type LookupFunc func(ctx context.Context, actionID string) (bool, error)

// Exists implements RecordLookup.
func (f LookupFunc) Exists(ctx context.Context, actionID string) (bool, error) {
	return f(ctx, actionID)
}

// SetLookup is a RecordLookup over a fixed set of action ids, for the fixture instance of the check.
type SetLookup map[string]bool

// Exists implements RecordLookup.
func (s SetLookup) Exists(_ context.Context, actionID string) (bool, error) { return s[actionID], nil }

// CompletenessResult is the outcome of one scan. Scanned is reported alongside Findings because a
// scan that examined nothing produces no findings, and "no findings" over an empty window reads
// exactly like a pass. 09's rule is that a check which could not run its property is `deferred` with
// a named blocker, never `pass` -- and the caller cannot apply that rule without this number.
type CompletenessResult struct {
	// Source is the audit source's name, so the verdict says what evidence it rests on.
	Source string
	// Window is the interval scanned.
	Since, Until time.Time
	// Scanned is how many mutating writes by actor identities were examined.
	Scanned int
	// Findings is every unjournaled write, sorted by time.
	Findings []Finding
}

// Passed reports whether the scan found nothing. It says nothing about whether the scan was
// MEANINGFUL -- see Scanned.
func (r CompletenessResult) Passed() bool { return len(r.Findings) == 0 }

// Summary is the one-line result for a check's output.
func (r CompletenessResult) Summary() string {
	verdict := "PASS"
	if !r.Passed() {
		verdict = "FAIL"
	}
	return fmt.Sprintf("%s V-BRK-003: %d write(s) by actor identities scanned from %s over [%s, %s); %d unjournaled",
		verdict, r.Scanned, r.Source,
		r.Since.UTC().Format(time.RFC3339), r.Until.UTC().Format(time.RFC3339), len(r.Findings))
}

// CheckCompleteness scans the audit stream for writes by the given actor identities and reports
// every one with no matching ActionRecord.
//
// actorPrincipals must be non-empty. Scanning every principal would sweep in kubelet, the
// controller-manager, and every human with a kubeconfig -- none of which are required to journal --
// and the resulting wall of findings would be indistinguishable from a broken check. Refusing is
// better than producing a result nobody can read.
func CheckCompleteness(ctx context.Context, src AuditSource, lookup RecordLookup, actorPrincipals []string, since, until time.Time) (CompletenessResult, error) {
	res := CompletenessResult{Source: src.Name(), Since: since.UTC(), Until: until.UTC()}
	if len(actorPrincipals) == 0 {
		return res, fmt.Errorf("journal: CheckCompleteness needs at least one actor principal; scanning all principals would sweep in identities that are not required to journal")
	}
	if err := src.Available(ctx); err != nil {
		// Unavailable is NOT a pass. It is the named blocker that makes the verdict `deferred`, and
		// returning it as an error rather than an empty result is what stops a caller from
		// mistaking silence for cleanliness.
		return res, fmt.Errorf("journal: audit source %q is unavailable: %w", src.Name(), err)
	}

	writes, err := src.Writes(ctx, since, until, actorPrincipals)
	if err != nil {
		return res, fmt.Errorf("journal: read audit writes from %q: %w", src.Name(), err)
	}

	// Memoize the lookup: a single action commonly writes several objects, and one Get per write
	// would turn a fan-out action into a burst of identical API calls.
	seen := map[string]bool{}
	for _, w := range writes {
		res.Scanned++
		if w.ActionID == "" {
			res.Findings = append(res.Findings, Finding{Kind: FindingUnlabeled, Write: w})
			continue
		}
		exists, ok := seen[w.ActionID]
		if !ok {
			exists, err = lookup.Exists(ctx, w.ActionID)
			if err != nil {
				return res, fmt.Errorf("journal: look up ActionRecord for action id %s: %w", w.ActionID, err)
			}
			seen[w.ActionID] = exists
		}
		if !exists {
			res.Findings = append(res.Findings, Finding{Kind: FindingUnmatched, Write: w})
		}
	}

	sort.SliceStable(res.Findings, func(i, j int) bool {
		return res.Findings[i].Write.At.Before(res.Findings[j].Write.At)
	})
	return res, nil
}
