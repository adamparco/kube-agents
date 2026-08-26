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

package broker

import (
	"context"
	"sync"

	"github.com/go-logr/logr"
)

// SecurityRecord is one refusal that carries security meaning (06 §4.1, 03 §8).
//
// Not every refusal produces one. A malformed body or a mistyped field is a bug; a reserved key,
// a bypass flag, a replayed nonce or a foreign caller is somebody -- or something prompt-injected
// into somebody's agent -- trying an authority path that does not exist. Only the second kind
// belongs in a stream an operator is paged on, because a stream that also carries the first kind
// is a stream nobody reads.
type SecurityRecord struct {
	// Reason is the machine-readable refusal reason, identical to the one returned to the caller.
	Reason string
	// Detail is the human-readable explanation.
	Detail string
	// Caller is the authenticated identity, or the peer's certificate subject when authentication
	// itself is what failed. Never a value taken from the request body.
	Caller string
	// RemoteAddr is the transport peer. Kept alongside Caller because the two disagreeing --
	// a valid identity from an unexpected address -- is itself the signal.
	RemoteAddr string
	// Path is the route the attempt was made against.
	Path string
	// TraceID correlates the attempt with the conversation that produced it, when the body parsed
	// far enough to yield one. Empty is normal and not suspicious.
	TraceID string
	// Key, when set, is the reserved or unknown key that triggered the refusal.
	Key string
}

// SecuritySink receives one SecurityRecord per security-relevant refusal. Implementations must not
// block the request path: a sink that stalls turns a refusal into a hang, and a hang on the
// mutating route is indistinguishable from the broker being down.
type SecuritySink interface {
	Security(ctx context.Context, rec SecurityRecord)
}

// LogSecuritySink writes each record to a logr.Logger at a fixed message, matching the house
// pattern in internal/router. A structured log line is the durable artifact: it is what the
// V-BRK-010 and V-BRK-021 checks assert on, and it survives the pod that emitted it because the
// node agent ships it.
type LogSecuritySink struct {
	Log logr.Logger
}

// Security logs the record's fields as structured key/values under one stable message, so a log
// filter can select the whole stream without matching on prose.
func (s LogSecuritySink) Security(_ context.Context, rec SecurityRecord) {
	s.Log.Info("broker security refusal",
		"reason", rec.Reason,
		"detail", rec.Detail,
		"caller", rec.Caller,
		"remoteAddr", rec.RemoteAddr,
		"path", rec.Path,
		"traceId", rec.TraceID,
		"key", rec.Key,
	)
}

// MemorySecuritySink collects records in memory for tests. It is in the non-test file on purpose:
// the conformance harness links it, and a sink that only exists under `go test` cannot be used by
// a check that runs against the shipped binary's package.
type MemorySecuritySink struct {
	mu      sync.Mutex
	records []SecurityRecord
}

// Security appends the record.
func (s *MemorySecuritySink) Security(_ context.Context, rec SecurityRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records = append(s.records, rec)
}

// Records returns a copy of everything collected so far.
func (s *MemorySecuritySink) Records() []SecurityRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]SecurityRecord, len(s.records))
	copy(out, s.records)
	return out
}

// Reset drops everything collected so far.
func (s *MemorySecuritySink) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records = nil
}
