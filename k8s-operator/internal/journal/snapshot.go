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
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// InlineSnapshotLimit is the 1 MiB threshold of 05 §1.2 / 06 §4.3. Above it the body goes to the
// blob sink and the CR keeps the digest, so no record approaches etcd's 1.5 MiB object limit.
//
// The margin between the two numbers is not slack to spend. A record carries a snapshot PER TARGET
// plus an undo plan that may embed the same bodies again, so the per-object limit is what keeps the
// SUM under etcd's ceiling. Raising this constant to "use the space available" is how a fan-out
// action discovers the limit at write time, with the mutation already applied.
const InlineSnapshotLimit = 1 << 20

// BlobSink is where snapshots too large to inline are written. It is an interface because the sink
// differs per environment -- a bucket in production, a temp directory under test -- and because a
// snapshot that cannot be persisted must fail the action, which is a lot easier to prove against a
// sink you can make fail on purpose.
type BlobSink interface {
	// Name identifies the sink in ObjectStoreRef.store, so a reader knows where to go looking.
	Name() string
	// Put stores body under key and returns the digest it computed. It must be content-addressed:
	// re-Putting identical bytes under the same key is a no-op, not an error.
	Put(ctx context.Context, key string, body []byte) (sha256hex string, err error)
	// Get retrieves a previously stored body. Undo replays from this.
	Get(ctx context.Context, key string) ([]byte, error)
}

// Digest is the canonical content digest used everywhere in the journal: lower-hex SHA-256 of the
// exact bytes stored. The CRD's pattern requires lower-hex, so an implementation that returned
// uppercase would be rejected at admission rather than silently accepted.
func Digest(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// Sanitize strips a live object down to what may be journaled (05 §1.2).
//
// Two removals, for two different reasons:
//
//   - `managedFields` is deleted because it is server-side-apply bookkeeping that can be several
//     times the size of the object it annotates. It is noise that would push honest snapshots over
//     the inline limit.
//   - A Secret's `data`/`stringData` is replaced by a PER-KEY DIGEST. The material never enters the
//     journal, so `kubectl get actionrecord -o yaml` -- which every reader identity may run -- can
//     never become a credential-exfiltration path. Undo of a Secret change therefore restores only
//     from a digest-matched value the broker still holds in the sink under the sink's own
//     encryption; the caveat is recorded on the undo plan rather than discovered at replay.
//
// Also removed: the `kubectl.kubernetes.io/last-applied-configuration` annotation, which is a second
// full copy of the object and, for a Secret, a copy that has NOT been through the digesting above.
// Leaving it would make the Secret redaction cosmetic.
func Sanitize(obj *unstructured.Unstructured) (*unstructured.Unstructured, error) {
	if obj == nil {
		return nil, fmt.Errorf("journal: cannot sanitize a nil object")
	}
	out := obj.DeepCopy()
	unstructured.RemoveNestedField(out.Object, "metadata", "managedFields")
	unstructured.RemoveNestedField(out.Object, "metadata", "annotations", "kubectl.kubernetes.io/last-applied-configuration")

	if out.GetKind() == "Secret" {
		for _, field := range []string{"data", "stringData"} {
			raw, found, err := unstructured.NestedMap(out.Object, field)
			if err != nil {
				return nil, fmt.Errorf("journal: read Secret %s: %w", field, err)
			}
			if !found {
				continue
			}
			digested := make(map[string]any, len(raw))
			for k, v := range raw {
				s, ok := v.(string)
				if !ok {
					// A non-string value here means the object is not a Secret in the shape we
					// think it is. Digest its JSON rather than pass the value through: the failure
					// mode we are guarding is "material reached the journal", and an unexpected
					// type is not a reason to relax it.
					b, mErr := json.Marshal(v)
					if mErr != nil {
						return nil, fmt.Errorf("journal: digest Secret %s[%q]: %w", field, k, mErr)
					}
					digested[k] = "sha256:" + Digest(b)
					continue
				}
				digested[k] = "sha256:" + Digest([]byte(s))
			}
			if err := unstructured.SetNestedMap(out.Object, digested, field); err != nil {
				return nil, fmt.Errorf("journal: write digested Secret %s: %w", field, err)
			}
		}
	}
	return out, nil
}

// Snapshot sanitizes obj, decides inline versus out-of-band by size, and returns the PreStateSnapshot
// the broker puts in spec.preState.
//
// If the body exceeds the inline limit and the sink write fails, this returns an error and the
// caller MUST NOT execute. That is the fail-closed rule of 03 §6 applied to the snapshot rather than
// to the record: an action executed without a recoverable pre-state is an action that cannot be
// undone, on a record that claims it can.
func Snapshot(ctx context.Context, sink BlobSink, targetIndex int32, obj *unstructured.Unstructured, keyPrefix string, capturedAt time.Time) (agentv1alpha1.PreStateSnapshot, error) {
	clean, err := Sanitize(obj)
	if err != nil {
		return agentv1alpha1.PreStateSnapshot{}, err
	}
	// Marshal through the unstructured map so the bytes are deterministic: encoding/json sorts map
	// keys, which is what makes the digest reproducible across brokers and across restarts.
	body, err := json.Marshal(clean.Object)
	if err != nil {
		return agentv1alpha1.PreStateSnapshot{}, fmt.Errorf("journal: marshal snapshot: %w", err)
	}
	digest := Digest(body)

	snap := agentv1alpha1.PreStateSnapshot{
		TargetIndex: targetIndex,
		CapturedAt:  metav1.NewTime(capturedAt.UTC()),
		SHA256:      digest,
	}

	if len(body) <= InlineSnapshotLimit {
		snap.Object = &runtime.RawExtension{Raw: body}
		return snap, nil
	}

	if sink == nil {
		return agentv1alpha1.PreStateSnapshot{}, fmt.Errorf(
			"journal: snapshot for target %d is %d bytes (over the %d-byte inline limit) and no blob sink is configured; refusing to execute an action whose pre-state cannot be persisted (03 §6)",
			targetIndex, len(body), InlineSnapshotLimit)
	}
	key := fmt.Sprintf("%s/%d", keyPrefix, targetIndex)
	stored, err := sink.Put(ctx, key, body)
	if err != nil {
		return agentv1alpha1.PreStateSnapshot{}, fmt.Errorf(
			"journal: persist %d-byte snapshot for target %d to sink %q: %w (the action must not execute)",
			len(body), targetIndex, sink.Name(), err)
	}
	if stored != digest {
		// The sink returning a different digest means it did not store what we handed it. Trusting
		// it would produce a record whose objectRef.sha256 no longer matches the body undo replays.
		return agentv1alpha1.PreStateSnapshot{}, fmt.Errorf(
			"journal: sink %q stored digest %s for a body digesting to %s; refusing to record a snapshot we cannot verify",
			sink.Name(), stored, digest)
	}
	snap.ObjectRef = &agentv1alpha1.ObjectStoreRef{Store: sink.Name(), Key: key, SHA256: digest}
	return snap, nil
}

// LoadSnapshot is the read half, used by undo. It resolves inline or out-of-band transparently and
// ALWAYS re-verifies the digest -- including for the inline case, where the body sits in etcd and a
// mismatch would mean the record was edited past the immutability rule. Verifying costs a hash of
// something already in memory and turns a silent wrong-world restore into a refusal.
func LoadSnapshot(ctx context.Context, sink BlobSink, snap agentv1alpha1.PreStateSnapshot) ([]byte, error) {
	var body []byte
	switch {
	case snap.Object != nil:
		body = snap.Object.Raw
	case snap.ObjectRef != nil:
		if sink == nil {
			return nil, fmt.Errorf("journal: snapshot for target %d lives in store %q but no blob sink is configured", snap.TargetIndex, snap.ObjectRef.Store)
		}
		if snap.ObjectRef.Store != sink.Name() {
			return nil, fmt.Errorf("journal: snapshot for target %d lives in store %q, but the configured sink is %q", snap.TargetIndex, snap.ObjectRef.Store, sink.Name())
		}
		var err error
		body, err = sink.Get(ctx, snap.ObjectRef.Key)
		if err != nil {
			return nil, fmt.Errorf("journal: read snapshot %q from sink %q: %w", snap.ObjectRef.Key, sink.Name(), err)
		}
	default:
		// The CRD's CEL rule makes this unreachable through the API server. It is still checked,
		// because the same struct is built in-process before it is ever submitted.
		return nil, fmt.Errorf("journal: snapshot for target %d carries neither an inline body nor an objectRef", snap.TargetIndex)
	}
	if got := Digest(body); got != snap.SHA256 {
		return nil, fmt.Errorf(
			"journal: snapshot for target %d digests to %s but the record says %s; refusing to replay an undo against a body that changed",
			snap.TargetIndex, got, snap.SHA256)
	}
	return body, nil
}

// SnapshotKeyPrefix is the content-addressing layout for the blob sink: identity, then action, so a
// human reading sink keys can see which agent produced what without joining anything.
func SnapshotKeyPrefix(agentIdentity, actionID string) string {
	return fmt.Sprintf("%s/%s", agentIdentity, actionID)
}

// SortedTargetIndices returns the target indices a snapshot list covers, sorted. Used by callers
// that need to check snapshot coverage against spec.targets without assuming order.
func SortedTargetIndices(snaps []agentv1alpha1.PreStateSnapshot) []int32 {
	out := make([]int32, 0, len(snaps))
	for _, s := range snaps {
		out = append(out, s.TargetIndex)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
