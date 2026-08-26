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
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// THE EXPORT IS THE DURABLE RECORD (05 §1.2). The CR in etcd is the working copy: it is watchable,
// queryable, and deleted after its TTL. The exported stream is what survives, which is why the
// retention controller may not delete a record the exporter has not confirmed, and why the SLIs are
// computed over the export rather than over the cluster.
//
// The 60-second budget in 05 §1.2 is a property of this path, not an aspiration: a phase transition
// that has not been exported within 60 s is a record that etcd garbage collection could destroy
// before it became durable.

// ExportEntry is one phase transition rendered for the audit sink. It is flat and self-describing --
// no references into the cluster -- because it is read months later by someone who has neither the
// cluster nor this code.
type ExportEntry struct {
	// Event is always `kube-agents.action` so a sink-side filter needs no schema knowledge.
	Event string `json:"event"`
	// At is when the exporter emitted the entry.
	At time.Time `json:"at"`
	// ActionID and Namespace locate the record while it still exists.
	ActionID  string `json:"actionId"`
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	// AgentIdentity, ActorServiceAccount and Requester are the attribution triple. All three are
	// exported: the identity says which agent, the SA says which credential actually wrote, and the
	// requester says which human asked. Investigations need all three and they are not derivable
	// from one another.
	AgentIdentity         string `json:"agentIdentity"`
	ActorServiceAccount   string `json:"actorServiceAccount"`
	Requester             string `json:"requester"`
	AttributionUnverified bool   `json:"attributionUnverified"`
	// Phase is the transition being recorded.
	Phase string `json:"phase"`
	// Class, DryRun, Intent, ChainID and UndoOf carry the rest of the decision.
	Class    string `json:"class"`
	DryRun   bool   `json:"dryRun"`
	Intent   string `json:"intent"`
	ChainID  string `json:"chainId"`
	UndoOf   string `json:"undoOf,omitempty"`
	UndoneBy string `json:"undoneBy,omitempty"`
	// Targets is rendered as strings; the full objects are in the record and, for large snapshots,
	// in the blob sink. The export is an index into evidence, not a second copy of it.
	Targets []string `json:"targets"`
	// Contested marks a change a human disagreed with (06 §4.4).
	Contested bool `json:"contested,omitempty"`
	// Message is the one-line human summary.
	Message string `json:"message,omitempty"`
	// ExpiresAt is when the CR will be garbage-collected, so a reader of the export knows how long
	// the working copy is still there to consult.
	ExpiresAt time.Time `json:"expiresAt"`
}

// AuditSink is the durable destination. It is an interface for the same reason AuditSource is: the
// customer chooses the sink (05 §1.2 says "Cloud Logging → a retention-locked bucket / BigQuery, or
// any customer sink"), and a test needs one it can inspect and one it can make fail.
type AuditSink interface {
	// Name identifies the sink, and lands in status.exported.sink so a reader can go and look
	// rather than trust a boolean.
	Name() string
	// Export must not return nil until the entry is durable. A sink that acknowledges early turns
	// the retention controller's post-export predicate into a no-op, and the failure is invisible
	// until someone goes looking for evidence that was deleted on schedule.
	Export(ctx context.Context, entry ExportEntry) error
}

// EntryFor renders a record's current phase as an export entry.
func EntryFor(ar *agentv1alpha1.ActionRecord, at time.Time) ExportEntry {
	targets := make([]string, 0, len(ar.Spec.Targets))
	for _, t := range ar.Spec.Targets {
		targets = append(targets, TargetString(t))
	}
	return ExportEntry{
		Event:                 "kube-agents.action",
		At:                    at.UTC(),
		ActionID:              ar.Spec.ActionID,
		Namespace:             ar.Namespace,
		Name:                  ar.Name,
		AgentIdentity:         ar.Spec.AgentIdentity,
		ActorServiceAccount:   ar.Spec.ActorServiceAccount,
		Requester:             ar.Spec.Requester.ID,
		AttributionUnverified: ar.Spec.AttributionUnverified,
		Phase:                 string(ar.Status.Phase),
		Class:                 string(ar.Spec.Classification.Class),
		DryRun:                ar.Spec.DryRun,
		Intent:                ar.Spec.Intent,
		ChainID:               ar.Spec.Trigger.ChainID,
		UndoOf:                ar.Spec.Trigger.UndoOf,
		UndoneBy:              ar.Status.UndoneBy,
		Targets:               targets,
		Contested:             ar.Status.Contested,
		Message:               ar.Status.Message,
		ExpiresAt:             ar.Spec.Retention.ExpiresAt.UTC(),
	}
}

// TargetString renders a TargetRef as group/version/Kind namespace/name, the form every log line and
// error message in this package uses.
func TargetString(t agentv1alpha1.TargetRef) string {
	gv := t.Version
	if t.Group != "" {
		gv = t.Group + "/" + t.Version
	}
	if t.Namespace == "" {
		return fmt.Sprintf("%s/%s %s", gv, t.Kind, t.Name)
	}
	return fmt.Sprintf("%s/%s %s/%s", gv, t.Kind, t.Namespace, t.Name)
}

// ---------------------------------------------------------------------------------------------
// Sinks
// ---------------------------------------------------------------------------------------------

// WriterSink emits one JSON object per line to an io.Writer. Pointed at os.Stdout on GKE this IS the
// Cloud Logging path: the node agent picks up container stdout, structured JSON is parsed into
// jsonPayload, and a log sink routes it to a retention-locked bucket or BigQuery. No client library,
// no credential, no egress hole -- which matters for a pod under a default-deny egress policy.
type WriterSink struct {
	name string
	mu   sync.Mutex
	w    io.Writer
}

// NewWriterSink returns a sink writing to w under the given name.
func NewWriterSink(name string, w io.Writer) *WriterSink { return &WriterSink{name: name, w: w} }

// Name implements AuditSink.
func (s *WriterSink) Name() string { return s.name }

// Export implements AuditSink. The mutex is not paranoia: the reconciler runs with concurrency > 1
// and interleaved partial writes would produce lines that are not JSON, which a sink drops silently.
func (s *WriterSink) Export(_ context.Context, entry ExportEntry) error {
	body, err := json.Marshal(entry)
	if err != nil {
		return fmt.Errorf("journal: marshal export entry for %s: %w", entry.ActionID, err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := s.w.Write(append(body, '\n')); err != nil {
		return fmt.Errorf("journal: write export entry for %s to sink %q: %w", entry.ActionID, s.name, err)
	}
	return nil
}

// MemorySink records entries in memory. Tests use it to assert what was exported; nothing else
// should, because "durable" is the one property it does not have.
type MemorySink struct {
	mu      sync.Mutex
	entries []ExportEntry
	// Err, when set, makes every Export fail. This is how the post-export deletion predicate is
	// tested: a sink that cannot acknowledge must leave records undeleted past their TTL rather
	// than let them age out unexported.
	Err error
}

// Name implements AuditSink.
func (m *MemorySink) Name() string { return "memory" }

// Export implements AuditSink.
func (m *MemorySink) Export(_ context.Context, entry ExportEntry) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.Err != nil {
		return m.Err
	}
	m.entries = append(m.entries, entry)
	return nil
}

// Entries returns a copy of what has been exported so far.
func (m *MemorySink) Entries() []ExportEntry {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]ExportEntry, len(m.entries))
	copy(out, m.entries)
	return out
}
