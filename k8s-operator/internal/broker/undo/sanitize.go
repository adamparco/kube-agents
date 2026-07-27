// Package undo generates the inverse of an action, before the action runs.
//
// 06 §4.3.1: the plan is produced at step 6, BEFORE execution, and each step is dry-run against the
// API server. Generating it afterwards would mean discovering an action is irreversible only once
// it is already done, which is the one moment the information is worthless.
//
// The load-bearing property of this package is not that it produces good plans. It is that when it
// CANNOT produce one it says so, and the classifier raises the action to `gated` (06 §4.2
// `no-undo-plan`, 09 V-REV-003). Every path here that gives up returns `StrategyNone` with a stated
// reason rather than an empty plan, a best-effort plan, or an error the caller might log and
// continue past.
package undo

import (
	"fmt"
	"sort"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

// droppedMetadataFields is the DROP list of 06 §4.3.1, under metadata.
//
// Every entry is a field the API SERVER owns. Replaying a snapshot that carries them is not merely
// noisy: `resourceVersion` makes the apply a conflicting update, `uid` is rejected outright on a
// create, and `managedFields` restores a server-side-apply ownership graph that no longer describes
// reality. The sanitizer exists so that `restore` is idempotent -- applying it twice is applying it
// once -- and so that a mirror diff shows what a human changed rather than what etcd incremented.
var droppedMetadataFields = []string{
	"uid",
	"resourceVersion",
	"generation",
	"creationTimestamp",
	"managedFields",
	"deletionTimestamp",
	"deletionGracePeriodSeconds",
	"selfLink",
}

// droppedAnnotations are annotations that describe a previous apply rather than the object.
//
// `last-applied-configuration` is a whole second copy of the object as it was at some earlier
// point. Keeping it would embed a stale snapshot inside a fresh one, and on restore it would become
// the base for the next three-way merge -- so a restore would be computed against a state that is
// neither the current one nor the one being restored.
var droppedAnnotations = []string{
	"kubectl.kubernetes.io/last-applied-configuration",
}

// droppedSpecFields are the fields the cluster ASSIGNS and will not accept back.
//
// These are immutable-once-set and allocated from a pool. `spec.clusterIP` is handed out by the
// apiserver; replaying an old one either collides with whatever holds it now or fails validation.
// The correct restore asks for a new allocation, which is why they are dropped rather than pinned.
// Each is a dotted path from the object root.
var droppedSpecFields = [][]string{
	{"spec", "clusterIP"},
	{"spec", "clusterIPs"},
	{"spec", "healthCheckNodePort"},
}

// Redaction records one Secret value that was replaced with a digest in the CR copy.
//
// The value itself is NOT in this struct and cannot be put in one. 06 §4.3.1 sends the restorable
// ciphertext to the journal store under `objectRef`, "never to the mirror repo, never to a log" --
// and a ledger, a chat notification and an audit trail are all logs. What travels here is the
// coordinate and the digest, which is enough to verify a restore and not enough to perform a leak.
type Redaction struct {
	// Key is the key within the Secret's data map.
	Key string
	// SHA256 is the lower-hex digest of the raw (decoded) value.
	SHA256 string
}

func (r Redaction) String() string { return fmt.Sprintf("data[%q] -> sha256:%s", r.Key, r.SHA256) }

// Sanitize returns a copy of obj with the 06 §4.3.1 DROP/KEEP/REDACT rules applied.
//
// This is THE sanitizer. 06 §3 requires the mirror's normalization to call "the §4.3.1 sanitizer --
// the same function, not a parallel one", and the reason is a failure that has already happened
// twice in this build under other names: two copies of one decision drift, and the drift is silent
// because each copy is individually correct. A second implementation here would mean a snapshot
// that round-trips through the undo path and a snapshot that round-trips through the mirror path
// disagreeing about what the object was, with no test comparing them.
//
// The input is not mutated. `isStatusTarget` is the one documented exception in the DROP list: when
// the action's target IS the status subresource, `status` is the payload and dropping it would
// produce a plan that restores nothing.
func Sanitize(obj *unstructured.Unstructured, isStatusTarget bool) (*unstructured.Unstructured, []Redaction, error) {
	if obj == nil {
		return nil, nil, fmt.Errorf("cannot sanitize a nil object")
	}
	if obj.GetKind() == "" {
		return nil, nil, fmt.Errorf("cannot sanitize an object with no kind: a snapshot that does not say what it is cannot be replayed")
	}
	if obj.GetName() == "" {
		return nil, nil, fmt.Errorf("cannot sanitize %s with no metadata.name: an undo step needs a target to address", obj.GetKind())
	}

	out := obj.DeepCopy()

	for _, f := range droppedMetadataFields {
		unstructured.RemoveNestedField(out.Object, "metadata", f)
	}

	if anns := out.GetAnnotations(); len(anns) > 0 {
		for _, a := range droppedAnnotations {
			delete(anns, a)
		}
		// An annotations map that is empty after the drop is removed entirely rather than left as
		// `{}`. The two are equal to the API server and unequal to a textual diff, and this object
		// ends up in a mirror commit a human reads.
		if len(anns) == 0 {
			unstructured.RemoveNestedField(out.Object, "metadata", "annotations")
		} else {
			out.SetAnnotations(anns)
		}
	}

	if !isStatusTarget {
		unstructured.RemoveNestedField(out.Object, "status")
	}

	for _, path := range droppedSpecFields {
		unstructured.RemoveNestedField(out.Object, path...)
	}
	if err := dropNodePorts(out); err != nil {
		return nil, nil, err
	}

	redactions, err := redactSecretData(out)
	if err != nil {
		return nil, nil, err
	}

	return out, redactions, nil
}

// dropNodePorts removes the assigned `nodePort` from every service port.
//
// The DROP list writes this as `.nodePort` -- a suffix, not a path -- because it does not live at a
// fixed location: it is one field of each element of `spec.ports`. Handled explicitly rather than
// by a recursive search for any key named `nodePort`, since a recursive strip would also reach into
// a CRD whose spec happens to use the word, and silently corrupt an object the sanitizer was only
// asked to normalize.
func dropNodePorts(out *unstructured.Unstructured) error {
	ports, found, err := unstructured.NestedFieldNoCopy(out.Object, "spec", "ports")
	if err != nil || !found {
		// A malformed spec.ports is not this function's problem to report: the object came from the
		// API server, so it parsed once already. Returning the error would turn every odd CRD that
		// uses `ports` for something else into an ungeneratable undo plan, and an ungeneratable plan
		// gates the action -- a spurious gate trains people to click approve.
		return nil
	}
	list, ok := ports.([]any)
	if !ok {
		return nil
	}
	for _, p := range list {
		if m, ok := p.(map[string]any); ok {
			delete(m, "nodePort")
		}
	}
	return nil
}

// redactSecretData replaces Secret values with digests IN THE CR COPY.
//
// Called for every object, not only for Secrets, and keyed on kind rather than on the presence of a
// `data` field -- a ConfigMap has `data` too, and its values are not secret. Keying on the field
// would redact every ConfigMap in the build and leave an undo plan that restores a ConfigMap full
// of hex.
//
// `stringData` is write-only: the API server folds it into `data` and never returns it, so a
// snapshot read back from the cluster cannot contain one. It is handled anyway, because a snapshot
// may also be constructed from a payload the agent submitted, and that one can.
func redactSecretData(out *unstructured.Unstructured) ([]Redaction, error) {
	if out.GetKind() != "Secret" || out.GetAPIVersion() != "v1" {
		return nil, nil
	}

	var redactions []Redaction
	for _, field := range []string{"data", "stringData"} {
		m, found, err := unstructured.NestedMap(out.Object, field)
		if err != nil {
			return nil, fmt.Errorf("Secret %s/%s has a malformed %s: %w", out.GetNamespace(), out.GetName(), field, err)
		}
		if !found {
			continue
		}
		redacted := make(map[string]any, len(m))
		for k, v := range m {
			s, _ := v.(string)
			d := digestOfSecretValue(field, s)
			redacted[k] = "sha256:" + d
			redactions = append(redactions, Redaction{Key: k, SHA256: d})
		}
		if err := unstructured.SetNestedMap(out.Object, redacted, field); err != nil {
			return nil, fmt.Errorf("Secret %s/%s: %w", out.GetNamespace(), out.GetName(), err)
		}
	}

	// Sorted so that two sanitizations of the same object produce byte-identical output. The
	// redaction list reaches a digest that is compared for equality; map iteration order would make
	// that digest differ between two runs over the same input.
	sort.Slice(redactions, func(i, j int) bool { return redactions[i].Key < redactions[j].Key })
	return redactions, nil
}

// SanitizedForDiff renders the object as a stable string for the mirror and for tests.
//
// Not a substitute for the JCS canonicalization the envelope uses -- that one is a wire format with
// a specification. This is a human-facing rendering, and it says so in its name so the two are not
// confused at a call site where only one of them is correct.
func SanitizedForDiff(obj *unstructured.Unstructured) string {
	if obj == nil {
		return ""
	}
	var b strings.Builder
	fmt.Fprintf(&b, "%s/%s %s/%s", obj.GetAPIVersion(), obj.GetKind(), obj.GetNamespace(), obj.GetName())
	return b.String()
}
