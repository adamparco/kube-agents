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
	"os/exec"
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
// Cloud Logging source (L2)
// ---------------------------------------------------------------------------------------------

// CloudLoggingSource reads the GKE Kubernetes API audit stream via the `gcloud` CLI.
//
// The CLI rather than the Go client library is a deliberate trade. The client would add
// cloud.google.com/go/logging and its transitive tree to an operator that otherwise talks only to
// the API server, for a code path that runs in verification and not on the request path. The CLI is
// already a hard dependency of every provisioning and verify script in this repository, and using it
// here means the L2 check and the L2 script read the same stream through the same filter.
type CloudLoggingSource struct {
	// Project is the GCP project whose logs are read.
	Project string
	// ClusterName scopes the query to one cluster.
	ClusterName string
	// GCloud is the binary to invoke; empty means "gcloud" from PATH. Injectable for tests.
	GCloud string
}

// Name implements AuditSource.
func (c *CloudLoggingSource) Name() string {
	return fmt.Sprintf("cloud-logging:%s/%s", c.Project, c.ClusterName)
}

func (c *CloudLoggingSource) bin() string {
	if c.GCloud != "" {
		return c.GCloud
	}
	return "gcloud"
}

// dataAccessFilter selects Kubernetes API mutations from the Data Access log. Admin Activity is NOT
// enough: it carries control-plane operations (and the leases the operator itself renews), not the
// object writes an agent's actor identity makes. That distinction IS planning defect 3.
func (c *CloudLoggingSource) dataAccessFilter(since, until time.Time) string {
	return fmt.Sprintf(
		`logName:"cloudaudit.googleapis.com%%2Fdata_access" AND resource.type="k8s_cluster" AND resource.labels.cluster_name=%q AND timestamp>=%q AND timestamp<%q`,
		c.ClusterName,
		since.UTC().Format(time.RFC3339),
		until.UTC().Format(time.RFC3339),
	)
}

// Available implements AuditSource. It reports the specific, actionable failure rather than a
// generic one: an empty Data Access stream on a project with no `auditConfigs` is not a broken
// query, it is a feature that was never switched on, and the two need different responses.
func (c *CloudLoggingSource) Available(ctx context.Context) error {
	until := time.Now().UTC()
	since := until.Add(-30 * 24 * time.Hour)
	out, err := c.read(ctx, c.dataAccessFilter(since, until), 1)
	if err != nil {
		// A read failure and an empty stream are different faults with the same remedy list, and the
		// caller turns either one into a `deferred` verdict with this string as the named blocker
		// (09 §9.6). A blocker a reader cannot act on gets waived, so the actionable half is
		// repeated here rather than left to the case below.
		return fmt.Errorf(
			"journal: could not read the Kubernetes Data Access audit stream for project %q, cluster %q: %w. "+
				"Check that gcloud is installed and authenticated, and that Data Access audit logs for "+
				"service k8s.io are enabled in the project's IAM auditConfigs -- they are OFF BY DEFAULT "+
				"(phase-9.md, planning defect 3)",
			c.Project, c.ClusterName, err)
	}
	if len(out) == 0 {
		return fmt.Errorf(
			"journal: no Kubernetes Data Access audit entries in project %q for cluster %q over the last 30 days. "+
				"Data Access audit logs for the Kubernetes API are OFF BY DEFAULT on GCP; enabling them is a "+
				"project-level IAM auditConfigs change for service k8s.io (phase-9.md, planning defect 3). "+
				"V-BRK-003 must run against the fixture source until this is on",
			c.Project, c.ClusterName)
	}
	return nil
}

// Writes implements AuditSource.
func (c *CloudLoggingSource) Writes(ctx context.Context, since, until time.Time, principals []string) ([]AuditWrite, error) {
	filter := c.dataAccessFilter(since, until)
	if len(principals) > 0 {
		quoted := make([]string, 0, len(principals))
		for _, p := range principals {
			quoted = append(quoted, fmt.Sprintf("protoPayload.authenticationInfo.principalEmail=%q", p))
		}
		filter += " AND (" + strings.Join(quoted, " OR ") + ")"
	}
	return c.read(ctx, filter, 1000)
}

// cloudLoggingEntry is the slice of the audit entry this source projects from. Every other field in
// a Kubernetes audit entry is deliberately dropped: pulling the request body into the operator's
// memory would mean an audit reader that can see Secret material.
type cloudLoggingEntry struct {
	Timestamp    time.Time `json:"timestamp"`
	ProtoPayload struct {
		MethodName         string `json:"methodName"`
		ResourceName       string `json:"resourceName"`
		AuthenticationInfo struct {
			PrincipalEmail string `json:"principalEmail"`
		} `json:"authenticationInfo"`
		Request struct {
			Metadata struct {
				Labels map[string]string `json:"labels"`
			} `json:"metadata"`
		} `json:"request"`
	} `json:"protoPayload"`
}

func (c *CloudLoggingSource) read(ctx context.Context, filter string, limit int) ([]AuditWrite, error) {
	// #nosec G204 -- the arguments are built from configuration, not from an agent-supplied value,
	// and exec.CommandContext does not go through a shell.
	cmd := exec.CommandContext(ctx, c.bin(), "logging", "read", filter,
		"--project", c.Project,
		"--limit", fmt.Sprint(limit),
		"--format", "json",
	)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	stdout, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("journal: gcloud logging read failed: %w: %s", err, strings.TrimSpace(stderr.String()))
	}
	var entries []cloudLoggingEntry
	if err := json.Unmarshal(stdout, &entries); err != nil {
		return nil, fmt.Errorf("journal: parse gcloud logging output: %w", err)
	}
	out := make([]AuditWrite, 0, len(entries))
	for _, e := range entries {
		verb := k8sVerbFromMethod(e.ProtoPayload.MethodName)
		if verb == "" {
			continue // a read; V-BRK-003 is about writes.
		}
		out = append(out, AuditWrite{
			At:        e.Timestamp,
			Principal: e.ProtoPayload.AuthenticationInfo.PrincipalEmail,
			Verb:      verb,
			Resource:  e.ProtoPayload.ResourceName,
			ActionID:  e.ProtoPayload.Request.Metadata.Labels[ActionIDLabel],
		})
	}
	return out, nil
}

// mutatingVerbs is the closed set V-BRK-003 cares about. `deletecollection` is here because a single
// entry can erase a namespace's worth of objects, and it is the verb most likely to be forgotten in
// a hand-written list.
var mutatingVerbs = map[string]bool{
	"create": true, "update": true, "patch": true, "delete": true, "deletecollection": true,
}

// k8sVerbFromMethod maps a Kubernetes audit methodName -- `io.k8s.apps.v1.deployments.patch` -- to
// its verb, returning "" for reads. The verb is the LAST dot-separated segment.
func k8sVerbFromMethod(method string) string {
	i := strings.LastIndex(method, ".")
	if i < 0 {
		return ""
	}
	verb := strings.ToLower(method[i+1:])
	if !mutatingVerbs[verb] {
		return ""
	}
	return verb
}
