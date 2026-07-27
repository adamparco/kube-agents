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
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// The idempotency key (06 §4.1).
//
// The key answers exactly one question: "is this the same WRITE as one I already did?" Everything
// in the envelope that is not part of that write is excluded, and the exclusions are the design.
//
//   - `intent` and `rationale` are model prose. Including them would mean a retry that reworded
//     itself -- which is what an LLM does on retry -- computed a different key and executed twice.
//   - `nonce` is single-use by construction, so including it would make every key unique and the
//     mechanism a no-op.
//   - `trace`, `requester`, `trigger`, `issuedAt` are provenance. The same write asked for twice
//     from two chat threads is still the same write.
//   - `requireApproval`, `maxObjects`, `deadlineSeconds` are how the write should be HANDLED, not
//     what it does.
//
// What remains -- who is writing, whether it is a dry run, and the operations themselves -- is the
// smallest set that distinguishes two different mutations. `dryRun` is in because a dry run and a
// real run of identical operations are emphatically not the same action, and deduplicating the
// second against the first would turn a rehearsal into a silent no-op where a mutation was
// expected. `agentIdentity` is in because two agents issuing the same operations are two actions
// with two accountabilities, and it comes from the authenticated caller, never the body.

// KeyPrefix is the algorithm tag. The key is `sha256:<64 lowerhex>`; the prefix exists so a future
// digest change is a visible format change rather than a silent one.
const KeyPrefix = "sha256:"

// keyInput is the K of 06 §4.1. Field names and order are part of the contract -- JCS sorts keys,
// so the struct order does not matter to the output, but the NAMES do, and renaming one here
// changes every key this broker computes.
type keyInput struct {
	AgentIdentity string         `json:"agentIdentity"`
	DryRun        bool           `json:"dryRun"`
	Operations    []keyOperation `json:"operations"`
}

// keyOperation is an operation reduced to what identifies the write. It is a separate type from
// Operation rather than a reuse of it so that adding a field to the wire envelope cannot silently
// change every idempotency key in flight: a new envelope field has to be added HERE, deliberately,
// to become part of the key.
type keyOperation struct {
	Op             string          `json:"op"`
	Target         *Target         `json:"target,omitempty"`
	TargetSelector *TargetSelector `json:"targetSelector,omitempty"`
	CloudTarget    *CloudTarget    `json:"cloudTarget,omitempty"`
	DesiredState   map[string]any  `json:"desiredState,omitempty"`
	Patch          *Patch          `json:"patch,omitempty"`
	Delete         *DeleteOptions  `json:"delete,omitempty"`
	Scale          *ScaleSpec      `json:"scale,omitempty"`
}

// ComputeIdempotencyKey derives the key from the authenticated identity and the envelope's
// operations, per 06 §4.1.
//
// The broker always recomputes rather than trusting the caller's value, and CompareIdempotencyKey
// rejects a mismatch. A caller-chosen key would be a dedup oracle: send a write with the key of an
// action you want suppressed and the broker returns the earlier record's outcome without ever
// performing yours -- or, in the other direction, vary the key on a replay and get the same write
// applied twice.
func ComputeIdempotencyKey(agentIdentity string, e *Envelope) (string, error) {
	if agentIdentity == "" {
		// Not a caller error -- it means the auth layer did not run, or ran and produced nothing.
		// A key computed over an empty identity would collide across every agent in the cluster.
		return "", fmt.Errorf("broker: cannot compute an idempotency key without an authenticated identity")
	}

	ops := make([]keyOperation, 0, len(e.Operations))
	for i := range e.Operations {
		op, err := reduceForKey(&e.Operations[i])
		if err != nil {
			return "", fmt.Errorf("broker: operation %d: %w", i, err)
		}
		ops = append(ops, op)
	}

	// Sorted, so the key does not depend on the order the caller happened to list the operations
	// in. Two envelopes applying the same three patches in different orders are the same write --
	// and a retry that reorders them (again: LLM) must not execute a second time.
	sort.SliceStable(ops, func(i, j int) bool { return operationSortKey(ops[i]) < operationSortKey(ops[j]) })

	raw, err := json.Marshal(keyInput{AgentIdentity: agentIdentity, DryRun: e.DryRun, Operations: ops})
	if err != nil {
		return "", fmt.Errorf("broker: marshal key input: %w", err)
	}
	canonical, err := Canonicalize(raw)
	if err != nil {
		return "", fmt.Errorf("broker: canonicalize key input: %w", err)
	}
	sum := sha256.Sum256(canonical)
	return KeyPrefix + hex.EncodeToString(sum[:]), nil
}

// operationSortKey is the ordering of 06 §4.1: `op + "\x1f" + group + "/" + version + "/" + kind +
// "/" + namespace + "/" + name`.
//
// US (0x1F, unit separator) rather than a printable delimiter because it cannot occur in any of
// the fields it joins -- a `/` or `:` could, and a separator that appears inside a field makes two
// distinct operations produce the same sort key.
//
// A selector and a cloud target have no `name`, so they fill the last slot with the thing that
// identifies them: the label selector, and provider/service/method. Without that, two selector
// operations differing only in their selector would sort equal and the sort's stability -- i.e.
// the caller's original order -- would leak back into the key.
func operationSortKey(o keyOperation) string {
	var group, version, kind, namespace, name string
	switch {
	case o.Target != nil:
		t := o.Target
		group, version, kind, namespace, name = t.Group, t.Version, t.Kind, t.Namespace, t.Name
	case o.TargetSelector != nil:
		s := o.TargetSelector
		group, version, kind, namespace, name = s.Group, s.Version, s.Kind, s.Namespace, s.LabelSelector
	case o.CloudTarget != nil:
		c := o.CloudTarget
		group, version, kind, namespace, name = c.Provider, "", c.Service, "", c.Resource+"#"+c.Method
	}
	return strings.Join([]string{o.Op, "\x1f", group, "/", version, "/", kind, "/", namespace, "/", name}, "")
}

// reduceForKey copies the identifying fields and puts every payload through the journal's §4.3.1
// sanitizer.
//
// It calls journal.Sanitize -- the same function the journal uses, not a parallel one. That is a
// correctness requirement, not tidiness: the key has to be stable against the redaction the
// journal performs, so if the two ever diverged, a record's stored payload and the payload its key
// was computed over would describe different writes, and no reader could reconcile them.
func reduceForKey(o *Operation) (keyOperation, error) {
	out := keyOperation{
		Op:             o.Op,
		Target:         o.Target,
		TargetSelector: o.TargetSelector,
		CloudTarget:    o.CloudTarget,
		Delete:         o.Delete,
		Scale:          o.Scale,
	}

	kind := ""
	if o.Target != nil {
		kind = o.Target.Kind
	} else if o.TargetSelector != nil {
		kind = o.TargetSelector.Kind
	}

	if o.DesiredState != nil {
		clean, err := sanitizePayload(o.DesiredState, kind)
		if err != nil {
			return keyOperation{}, err
		}
		out.DesiredState = clean
	}

	if o.Patch != nil {
		body, err := sanitizePatchBody(o.Patch, kind)
		if err != nil {
			return keyOperation{}, err
		}
		out.Patch = &Patch{Type: o.Patch.Type, Body: body}
	}
	return out, nil
}

// sanitizePayload runs a bare payload map through journal.Sanitize.
//
// The kind has to be injected because Sanitize decides whether to digest `data`/`stringData` by
// looking at the object's own `kind`, and an operation payload frequently omits it -- a
// merge-patch body is `{"data":{...}}` with no apiVersion and no kind at all. Without the
// injection a Secret patch would pass through undigested and the key would be computed over
// credential material. It is removed again afterwards so the injected field cannot become part of
// the key: a payload that did declare its kind and one that relied on the target's would
// otherwise produce different keys for the same write.
func sanitizePayload(payload map[string]any, kind string) (map[string]any, error) {
	obj := &unstructured.Unstructured{Object: payload}
	injected := false
	if obj.GetKind() == "" && kind != "" {
		obj = obj.DeepCopy()
		obj.SetKind(kind)
		injected = true
	}
	clean, err := journal.Sanitize(obj)
	if err != nil {
		return nil, fmt.Errorf("sanitize payload: %w", err)
	}
	if injected {
		unstructured.RemoveNestedField(clean.Object, "kind")
	}
	return clean.Object, nil
}

// sanitizePatchBody handles the three patch media types.
//
// Merge-patch and apply-patch bodies are objects and go straight through sanitizePayload. A JSON
// Patch body is an ARRAY of operations, which Sanitize has no shape for -- so each op's `value` is
// digested individually with journal.Digest, the same primitive Sanitize itself uses, when the op
// writes under a Secret's `/data` or `/stringData`. This is the one place the redaction is applied
// by path rather than by field, because in a JSON Patch the field name lives in the `path` string
// and nowhere else.
func sanitizePatchBody(p *Patch, kind string) (any, error) {
	switch body := p.Body.(type) {
	case map[string]any:
		return sanitizePayload(body, kind)

	case []any:
		if kind != "Secret" {
			return body, nil
		}
		out := make([]any, 0, len(body))
		for _, entry := range body {
			op, ok := entry.(map[string]any)
			if !ok {
				out = append(out, entry)
				continue
			}
			path, _ := op["path"].(string)
			if !strings.HasPrefix(path, "/data/") && !strings.HasPrefix(path, "/stringData/") {
				out = append(out, entry)
				continue
			}
			value, present := op["value"]
			if !present {
				// A `remove` op names a key but carries no material.
				out = append(out, entry)
				continue
			}
			digested := make(map[string]any, len(op))
			for k, v := range op {
				digested[k] = v
			}
			b, err := json.Marshal(value)
			if err != nil {
				return nil, fmt.Errorf("digest JSON Patch value at %s: %w", path, err)
			}
			digested["value"] = KeyPrefix + journal.Digest(b)
			out = append(out, digested)
		}
		return out, nil

	default:
		return body, nil
	}
}

// CompareIdempotencyKey recomputes the key and refuses a mismatch (06 §4.1).
//
// The refusal is a 400 and is NOT journaled as a security event. A mismatch is overwhelmingly a
// client bug -- an SDK that canonicalises numbers differently, a caller that hashed the intent by
// mistake -- and treating every one as an attack would bury the reserved-key events that are.
func CompareIdempotencyKey(agentIdentity string, e *Envelope) error {
	want, err := ComputeIdempotencyKey(agentIdentity, e)
	if err != nil {
		return err
	}
	if want != e.IdempotencyKey {
		return &Refusal{
			Status: http.StatusBadRequest,
			Reason: ReasonIdempotencyKeyMismatch,
			Detail: fmt.Sprintf(
				"idempotencyKey does not match the broker's recomputation over {agentIdentity, dryRun, operations}: sent %s, expected %s",
				e.IdempotencyKey, want),
		}
	}
	return nil
}
