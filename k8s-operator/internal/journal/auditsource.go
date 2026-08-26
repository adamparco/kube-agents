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
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

// V-BRK-003 -- "every audit-log write by an actor identity has a matching ActionRecord" -- is the
// check that closes the loop. Everything else in this package proves the journal is well-formed;
// only this proves the journal is COMPLETE. A broker that quietly writes without journaling passes
// every other check in the suite, because every other check reads the journal.
//
// The source is an interface rather than a Cloud Logging client for a reason recorded at PLAN time
// (phase-9.md, planning defect 3): GKE does not expose API-server audit configuration to the
// customer, the stream lands in Cloud Logging, and Data Access audit logs for the Kubernetes API are
// OFF BY DEFAULT -- turning them on is a project-level IAM policy change. V-BRK-003 is
// BLOCKING-ALWAYS and may not be deferred (09 §9.6), so the check has to be able to run somewhere
// before it can run everywhere. With this seam the L1 instance runs against a fixture stream and the
// L2 instance against Cloud Logging, and the reconciler is the same code in both.

// AuditWrite is one mutating API-server call as the audit stream saw it. It is deliberately a small
// projection of the audit entry rather than the entry itself: the reconciler needs four facts, and a
// struct mirroring Cloud Logging's schema would make the fixture source a fiction.
type AuditWrite struct {
	// At is when the API server recorded the call.
	At time.Time `json:"at"`
	// Principal is the authenticated identity, e.g. the actor service account.
	Principal string `json:"principal"`
	// Verb is the mutating verb: create, update, patch, delete, deletecollection.
	Verb string `json:"verb"`
	// Resource is the object written, in group/version/kind/namespace/name form as the source
	// rendered it. Free-form on purpose -- it appears in findings for a human, never in a join.
	Resource string `json:"resource"`
	// ActionID is the `kube-agents/action-id` the write carried, empty when it carried none. An
	// EMPTY value here is the finding: 05 §1.1 has the admission policy reject any write from an
	// actor identity without one, so an empty id in the stream means either the policy is not
	// installed or something wrote around it.
	ActionID string `json:"actionId,omitempty"`
}

// AuditSource is a readable stream of mutating writes. Implementations must be safe to call
// repeatedly with overlapping windows; the reconciler deduplicates.
type AuditSource interface {
	// Name identifies the source in logs and findings, so a green V-BRK-003 says WHICH stream it
	// was green against. A check that does not name its evidence is a check you cannot audit.
	Name() string
	// Available reports whether the stream can be read at all. It is separate from Writes so that
	// "the stream is off" is distinguishable from "the stream is on and empty" -- the two look
	// identical in a result set and mean opposite things.
	Available(ctx context.Context) error
	// Writes returns every mutating write in [since, until) by any of the given principals. An
	// empty principals slice means all principals.
	Writes(ctx context.Context, since, until time.Time, principals []string) ([]AuditWrite, error)
}

// ---------------------------------------------------------------------------------------------
// Fixture source (L1)
// ---------------------------------------------------------------------------------------------

// FixtureSource reads AuditWrite records as JSON Lines. It is the L1 instance of V-BRK-003 and the
// only way to exercise the NEGATIVE control -- an unjournaled write by an actor identity -- without
// arranging for a real agent to misbehave against a real cluster.
type FixtureSource struct {
	// Path is the JSONL file. One AuditWrite per line; blank lines and #-comments are skipped so a
	// fixture can explain what each case is for.
	Path string
}

// NewFixtureSource returns a source over the given JSONL file.
func NewFixtureSource(path string) *FixtureSource { return &FixtureSource{Path: path} }

// Name implements AuditSource.
func (f *FixtureSource) Name() string { return "fixture:" + f.Path }

// Available implements AuditSource.
func (f *FixtureSource) Available(context.Context) error {
	if _, err := os.Stat(f.Path); err != nil {
		return fmt.Errorf("journal: audit fixture %q is unreadable: %w", f.Path, err)
	}
	return nil
}

// Writes implements AuditSource.
func (f *FixtureSource) Writes(_ context.Context, since, until time.Time, principals []string) ([]AuditWrite, error) {
	fh, err := os.Open(f.Path)
	if err != nil {
		return nil, fmt.Errorf("journal: open audit fixture %q: %w", f.Path, err)
	}
	defer func() { _ = fh.Close() }()
	return parseAuditJSONL(fh, since, until, principals)
}

// parseAuditJSONL is shared by the fixture source and its tests. A malformed line is an error, not a
// skip: a fixture that silently drops the case it was written to cover is the shape of a check that
// looks green because it ran nothing.
func parseAuditJSONL(r io.Reader, since, until time.Time, principals []string) ([]AuditWrite, error) {
	want := make(map[string]bool, len(principals))
	for _, p := range principals {
		want[p] = true
	}
	var out []AuditWrite
	sc := bufio.NewScanner(r)
	// Audit entries carry a resource path and can be long; the default 64 KiB token is not enough.
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for line := 1; sc.Scan(); line++ {
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		var w AuditWrite
		if err := json.Unmarshal([]byte(text), &w); err != nil {
			return nil, fmt.Errorf("journal: audit fixture line %d is not a JSON AuditWrite: %w", line, err)
		}
		if len(want) > 0 && !want[w.Principal] {
			continue
		}
		if w.At.Before(since) || !w.At.Before(until) {
			continue
		}
		out = append(out, w)
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("journal: read audit fixture: %w", err)
	}
	return out, nil
}

// ---------------------------------------------------------------------------------------------
// Cloud Logging source (L2) -- moved out of this package
// ---------------------------------------------------------------------------------------------
//
// The L2 instance of AuditSource is `internal/journal/cloudaudit`. It is not here because it shells
// out to `gcloud`, this package is linked into the broker, and 08 §2.1/§2.6 put the broker on the
// smallest possible supply chain with no shell -- which for a Go binary means no `os/exec`, not
// merely no `/bin/sh` in the image. V-RUN-010 asserts it; see that package's doc comment.
//
// Nothing in this file may import `os/exec`, `plugin`, `net/http/pprof` or an inference client. The
// rule is mechanical, applies to every first-party package `cmd/broker` can reach, and is checked
// by `dev/tests/broker-supply-chain-minimal.py`.
