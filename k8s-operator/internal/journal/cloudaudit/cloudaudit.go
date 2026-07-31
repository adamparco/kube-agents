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

// Package cloudaudit is the L2 instance of the V-BRK-003 audit stream: the GKE Kubernetes API Data
// Access log, read through the `gcloud` CLI.
//
// # Why this is a package of its own
//
// It lives here rather than beside FixtureSource in `internal/journal` because it imports
// `os/exec`, and `internal/journal` is linked into the BROKER. 08 §2.1 puts the broker on "the
// smallest possible supply chain" and 08 §2.6 gives it no shell, and a Go binary's shell is
// `os/exec`: the image can be distroless and shell-free while the process retains the ability to
// fork one, which is the half of the property an image scan cannot see. Until P9-T9b-3 the broker
// binary carried this file's `exec.CommandContext` -- unreachable, since nothing in the broker
// constructs a CloudLoggingSource, but one call site away from reachable, in the one process in
// the mesh whose ServiceAccount can write. Nothing in the image, the SBOM or the RBAC would have
// shown it; V-RUN-010 (`dev/tests/broker-supply-chain-minimal.py`) is what shows it, and this
// package boundary is what keeps it shown.
//
// The consumer is the V-BRK-003 reconciler running in the operator or in an L2 driver, never the
// broker. Anything that imports this package must stay out of `cmd/broker`'s reachable set.
//
// # Why the CLI and not the client library
//
// A deliberate trade, unchanged by the move. The Go client would add cloud.google.com/go/logging
// and its transitive tree to an operator that otherwise talks only to the API server, for a code
// path that runs in verification and not on the request path. The CLI is already a hard dependency
// of every provisioning and verify script in this repository, and using it here means the L2 check
// and the L2 script read the same stream through the same filter.
package cloudaudit

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Source reads the GKE Kubernetes API audit stream via the `gcloud` CLI.
type Source struct {
	// Project is the GCP project whose logs are read.
	Project string
	// ClusterName scopes the query to one cluster.
	ClusterName string
	// GCloud is the binary to invoke; empty means "gcloud" from PATH. Injectable for tests.
	GCloud string
}

// The interface is asserted here, at the package boundary, rather than left to the reconciler's
// call site. Splitting an implementation away from the interface it satisfies is exactly when the
// two drift, and the drift surfaces as a build failure in whichever unit next wires V-BRK-003 --
// far from the edit that caused it.
var _ journal.AuditSource = (*Source)(nil)

// Name implements journal.AuditSource.
func (c *Source) Name() string {
	return fmt.Sprintf("cloud-logging:%s/%s", c.Project, c.ClusterName)
}

func (c *Source) bin() string {
	if c.GCloud != "" {
		return c.GCloud
	}
	return "gcloud"
}

// dataAccessFilter selects Kubernetes API mutations from the Data Access log. Admin Activity is NOT
// enough: it carries control-plane operations (and the leases the operator itself renews), not the
// object writes an agent's actor identity makes. That distinction IS planning defect 3.
func (c *Source) dataAccessFilter(since, until time.Time) string {
	return fmt.Sprintf(
		`logName:"cloudaudit.googleapis.com%%2Fdata_access" AND resource.type="k8s_cluster" AND resource.labels.cluster_name=%q AND timestamp>=%q AND timestamp<%q`,
		c.ClusterName,
		since.UTC().Format(time.RFC3339),
		until.UTC().Format(time.RFC3339),
	)
}

// Available implements journal.AuditSource. It reports the specific, actionable failure rather than
// a generic one: an empty Data Access stream on a project with no `auditConfigs` is not a broken
// query, it is a feature that was never switched on, and the two need different responses.
func (c *Source) Available(ctx context.Context) error {
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

// Writes implements journal.AuditSource.
func (c *Source) Writes(ctx context.Context, since, until time.Time, principals []string) ([]journal.AuditWrite, error) {
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

func (c *Source) read(ctx context.Context, filter string, limit int) ([]journal.AuditWrite, error) {
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
	out := make([]journal.AuditWrite, 0, len(entries))
	for _, e := range entries {
		verb := k8sVerbFromMethod(e.ProtoPayload.MethodName)
		if verb == "" {
			continue // a read; V-BRK-003 is about writes.
		}
		out = append(out, journal.AuditWrite{
			At:        e.Timestamp,
			Principal: e.ProtoPayload.AuthenticationInfo.PrincipalEmail,
			Verb:      verb,
			Resource:  e.ProtoPayload.ResourceName,
			ActionID:  e.ProtoPayload.Request.Metadata.Labels[journal.ActionIDLabel],
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
