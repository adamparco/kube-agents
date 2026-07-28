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

// Package broker implements the Action Broker (06 §4.1, 03 §4.1) -- the only process in
// kube-agents that holds a write credential, and therefore the only place a mutation can be
// authorized. Its whole design premise is that the caller is not trusted: the agent is an LLM,
// its output is attacker-influenceable (03 §8), and the broker's job is to be the deterministic
// code between that output and the API server.
package broker

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"
)

// The envelope's own type meta. Checked rather than ignored: a body that does not say what it is
// will be read by the NEXT version of this broker under different rules, and a caller that never
// declared a version has no way to be told it changed.
const (
	APIVersion   = "kubeagents.x-k8s.io/v1alpha1"
	EnvelopeKind = "ActionEnvelope"
)

// Limits from the 06 §4.1 field reference. They are constants rather than literals because the
// classifier, the fixtures and the tests all have to agree on the same numbers.
const (
	MaxIntentLen           = 512
	MaxRationaleLen        = 4096
	MaxOperations          = 50
	MaxIdempotencyKeyLen   = 128
	MaxDeadlineSeconds     = 900
	MinDeadlineSeconds     = 1
	DefaultDeadlineSeconds = 120
	DefaultMaxObjects      = 1
)

// Envelope is the request body an agent POSTs to its own broker (06 §4.1).
//
// Every field here is either DATA THE BROKER USES or PROVENANCE THE BROKER RECORDS. Nothing in it
// is authority: there is no tier, no scope, no risk class and no approval state, and those names
// are refused rather than ignored (see ReservedKeys). That is not defensive coding, it is the
// contract -- 03 §4.1 step 1 derives (tier, scope) from the authenticated identity, so an envelope
// field claiming either could only ever be an attempt to override it.
type Envelope struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`

	Intent string `json:"intent"`
	// Rationale is model reasoning. It is recorded and NEVER read by the classifier: a class
	// derived from prose an attacker can influence is not a control (03 §8).
	Rationale string `json:"rationale,omitempty"`

	Operations []Operation `json:"operations"`

	Requester Requester `json:"requester"`
	Trigger   Trigger   `json:"trigger"`
	Trace     Trace     `json:"trace"`

	IssuedAt       string `json:"issuedAt"`
	Nonce          string `json:"nonce"`
	IdempotencyKey string `json:"idempotencyKey"`

	DryRun          bool `json:"dryRun,omitempty"`
	RequireApproval bool `json:"requireApproval,omitempty"`

	// Pointers, so "absent" and "explicitly zero" are different. maxObjects: 0 is a caller error
	// worth reporting; with a plain int it would be indistinguishable from omission and would
	// silently become the default 1 -- a cap the caller did not ask for, applied to a fan-out.
	MaxObjects      *int `json:"maxObjects,omitempty"`
	DeadlineSeconds *int `json:"deadlineSeconds,omitempty"`
}

// Operation is one write. The three target shapes and the four payload shapes are mutually
// exclusive by op, and that exclusivity is validated rather than assumed -- an envelope carrying
// both a `target` and a `targetSelector` has two readings, and picking one silently is how a
// single-object patch becomes a fan-out.
type Operation struct {
	Op string `json:"op"`

	Target         *Target         `json:"target,omitempty"`
	TargetSelector *TargetSelector `json:"targetSelector,omitempty"`
	CloudTarget    *CloudTarget    `json:"cloudTarget,omitempty"`

	DesiredState map[string]any `json:"desiredState,omitempty"`
	Patch        *Patch         `json:"patch,omitempty"`
	Delete       *DeleteOptions `json:"delete,omitempty"`
	Scale        *ScaleSpec     `json:"scale,omitempty"`
}

// Target is a single Kubernetes object. `group: ""` is core.
type Target struct {
	Group     string `json:"group,omitempty"`
	Version   string `json:"version"`
	Kind      string `json:"kind"`
	Namespace string `json:"namespace,omitempty"`
	Name      string `json:"name"`
}

// TargetSelector is the fan-out form. It is expanded against LIVE state before classification
// (06 §4.2 blast radius), because a selector that matches three objects today and three hundred
// tomorrow is a risk class that changes without the envelope changing.
type TargetSelector struct {
	Group         string `json:"group,omitempty"`
	Version       string `json:"version"`
	Kind          string `json:"kind"`
	Namespace     string `json:"namespace"`
	LabelSelector string `json:"labelSelector"`
}

// CloudTarget is the non-Kubernetes variant (06 §4.1).
type CloudTarget struct {
	Provider string `json:"provider"`
	Service  string `json:"service"`
	Resource string `json:"resource"`
	Method   string `json:"method"`
}

// Patch carries one of the three accepted media types. Body is `any` and not a map on purpose:
// a JSON Patch body is an ARRAY, and a map-typed field would have rejected every valid one.
type Patch struct {
	Type string `json:"type"`
	Body any    `json:"body"`
}

// DeleteOptions mirrors the subset of metav1.DeleteOptions an agent may set.
type DeleteOptions struct {
	PropagationPolicy  string              `json:"propagationPolicy,omitempty"`
	GracePeriodSeconds *int64              `json:"gracePeriodSeconds,omitempty"`
	Preconditions      *DeletePrecondition `json:"preconditions,omitempty"`
}

// DeletePrecondition is the "delete this object, not the one that replaced it" guard.
type DeletePrecondition struct {
	UID             string `json:"uid,omitempty"`
	ResourceVersion string `json:"resourceVersion,omitempty"`
}

// ScaleSpec is the payload for op: scale.
type ScaleSpec struct {
	Replicas *int32 `json:"replicas"`
}

// Requester is attribution, NOT authorization (06 §2a). An unsigned `id` is recorded with
// attributionUnverified so a reader months later cannot mistake a claim for a fact.
type Requester struct {
	Kind        string `json:"kind"`
	ID          string `json:"id"`
	Platform    string `json:"platform,omitempty"`
	DisplayName string `json:"displayName,omitempty"`
	Assertion   string `json:"assertion,omitempty"`
}

// Trigger says what caused this action. `source` drives the autonomy metrics of 01 §7, which is
// why it is a closed enum: a free-text source would make "how much of this was self-initiated?"
// unanswerable a quarter later.
type Trigger struct {
	Source string `json:"source"`
	Ref    string `json:"ref,omitempty"`
	Detail string `json:"detail,omitempty"`
}

// Trace is the correlation triple. traceId is required because the §8 causal chain is built from
// it, and mechanism 3 of the anti-replay rules is keyed on it.
type Trace struct {
	TraceID   string `json:"traceId"`
	SpanID    string `json:"spanId,omitempty"`
	SessionID string `json:"sessionId,omitempty"`
	ThreadID  string `json:"threadId,omitempty"`
}

// Closed enums, as sets rather than switch statements so the validator and the fixtures can share
// them and a test can enumerate every accepted value.
var (
	validOps = map[string]bool{
		"create": true, "apply": true, "patch": true, "delete": true, "scale": true,
	}
	validPatchTypes = map[string]bool{
		"application/merge-patch+json": true,
		"application/json-patch+json":  true,
		"application/apply-patch+yaml": true,
	}
	validRequesterKinds = map[string]bool{"human": true, "agent": true, "system": true}
	validPlatforms      = map[string]bool{
		"": true, "slack": true, "googlechat": true, "kubectl": true, "mesh": true,
	}
	validTriggerSources = map[string]bool{
		"chat": true, "watch": true, "alert": true, "cron": true,
		"delegation": true, "escalation": true, "undo": true,
	}
	validPropagation = map[string]bool{
		"": true, "Foreground": true, "Background": true, "Orphan": true,
	}
)

// ReservedKeys are the top-level names an envelope may not carry, each mapped to the reason it is
// refused. This is the security-load-bearing half of the schema (06 §4.1), and it is a REFUSAL
// rather than a silent drop for one reason: an agent that has been talked into trying
// `"bypass": true` should leave evidence, and a dropped field leaves none. The names exist here
// only to be rejected loudly.
//
// Note what is NOT here: `target.namespace` is legitimate and common. Only the TOP level is
// reserved, exactly as 06 §4.1 says ("These are reserved top-level keys").
var ReservedKeys = map[string]string{
	// Authority. Derived from the authenticated caller, never from the body (03 §4.1 step 1).
	"tier":      "scope and tier come from the authenticated caller, never from the envelope",
	"scope":     "scope and tier come from the authenticated caller, never from the envelope",
	"namespace": "the target namespace belongs on each operation's target, not at the top level",
	"actor":     "the actor identity is the broker's own credential, not a caller choice",

	// Classification. Computed, never asserted.
	"riskClass": "risk is classified by the broker, never declared by the caller",
	"class":     "risk is classified by the broker, never declared by the caller",
	"severity":  "risk is classified by the broker, never declared by the caller",
	"approved":  "approval is recorded by the ChatOps gateway against the roster, never self-declared",

	// The bypass family.
	"bypass":      "there is no bypass",
	"force":       "there is no force",
	"skipJournal": "journaling is not optional; an action that cannot be journaled does not execute",
	"skipVerify":  "verification is not optional",
	"emergency":   "there is no emergency path; a gated action is gated",

	// Undo poisoning.
	"undoPlan": "the broker generates the undo plan; a caller-supplied one is an undo-poisoning vector",
}

// bypassFamily is the subset whose mere presence is a security event of its own character: these
// names have no innocent reading. Distinguished from the rest so the event can say which.
var bypassFamily = map[string]bool{
	"bypass": true, "force": true, "skipJournal": true, "skipVerify": true, "emergency": true,
	"approved": true, "undoPlan": true,
}

var (
	hex32Re          = regexp.MustCompile(`^[0-9a-f]{32}$`)
	sha256KeyRe      = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	labelSelectorBad = regexp.MustCompile(`^\s*$`)
)

// Refusal is a rejection with everything the caller and the journal each need, in one value.
//
// It carries `Journal` and `SecurityEvent` as fields rather than leaving them to the caller
// because 06 §4.1 assigns them PER REASON -- a replay is journaled and alarmed, an unknown field
// is neither -- and a switch at the call site would drift from the table the moment a reason is
// added.
type Refusal struct {
	Status        int    // HTTP status
	Reason        string // machine-readable, and the `reason` on the Rejected ActionRecord
	Detail        string // human-readable; safe to return to the caller
	Journal       bool   // write a Rejected ActionRecord
	SecurityEvent bool   // emit a security event

	// RetryAfterSeconds is how long the caller should wait before trying again. Zero means "do not
	// retry", which is the right answer for every schema and authorization refusal: a malformed
	// envelope is not going to become well-formed on its own.
	//
	// It lives here rather than being computed at the write site for the same reason Journal does:
	// 06 §4.4 puts `retryAfterSeconds` on the pause refusal, the brake is what knows whether a
	// refusal is temporary, and a value derived at the HTTP boundary would be derived from the
	// status code -- which cannot distinguish a freeze that clears in an hour from one with no
	// expiry at all.
	RetryAfterSeconds int
}

func (r *Refusal) Error() string { return r.Reason + ": " + r.Detail }

// The reason strings. Named constants because they appear in the HTTP body, the ActionRecord, the
// security event and the conformance checks, and a typo in any one of those is a check that
// silently stops matching.
const (
	ReasonReservedKey            = "reserved-key"
	ReasonBypassKey              = "bypass-key"
	ReasonUnknownField           = "unknown-field"
	ReasonMalformed              = "malformed-envelope"
	ReasonInvalid                = "invalid-envelope"
	ReasonEnvelopeExpired        = "envelope-expired"
	ReasonReplayedEnvelope       = "replayed-envelope"
	ReasonIdempotencyKeyMismatch = "idempotency-key-mismatch"
	ReasonScopeSpoof             = "scope-spoofed"
	ReasonForbiddenCaller        = "forbidden-caller"

	// The brake (06 §4.4). Same list, deliberately, rather than a second const block in brake.go:
	// these strings are a single namespace as far as a caller matching on `reason` is concerned,
	// and two definition sites is how the same string ends up meaning two things.
	ReasonAgentPaused        = "agent-paused"
	ReasonScopeFrozen        = "scope-frozen"
	ReasonTargetContested    = "target-contested"
	ReasonJournalUnavailable = "journal-unavailable"
	ReasonSnapshotFailed     = "snapshot-failed"
	ReasonBudgetExhausted    = "budget-exhausted"
	ReasonFlapDetected       = "flap-detected"
)

func invalid(format string, args ...any) *Refusal {
	return &Refusal{Status: http.StatusBadRequest, Reason: ReasonInvalid, Detail: fmt.Sprintf(format, args...)}
}

// DecodeEnvelope parses and validates a request body against the closed 06 §4.1 schema.
//
// The order matters and is not the obvious one. Reserved keys are checked BEFORE strict decoding,
// because `json.Decoder.DisallowUnknownFields` reports the first unknown field it happens to
// reach and reports all of them identically -- so an envelope carrying `bypass: true` would come
// back as a generic "unknown field", losing both the security event and the loud refusal that are
// the entire point of the reserved list.
func DecodeEnvelope(body []byte) (*Envelope, error) {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(body, &top); err != nil {
		return nil, &Refusal{
			Status: http.StatusBadRequest,
			Reason: ReasonMalformed,
			Detail: "body is not a JSON object: " + err.Error(),
		}
	}

	// Sorted, so a body carrying three reserved keys refuses the same one every time. A refusal
	// that names a different field per request is a refusal nobody can write a test against.
	var found []string
	for k := range top {
		if _, ok := ReservedKeys[k]; ok {
			found = append(found, k)
		}
	}
	if len(found) > 0 {
		sort.Strings(found)
		k := found[0]
		reason := ReasonReservedKey
		if bypassFamily[k] {
			reason = ReasonBypassKey
		}
		return nil, &Refusal{
			Status:        http.StatusBadRequest,
			Reason:        reason,
			Detail:        fmt.Sprintf("envelope carries the reserved key %q: %s", k, ReservedKeys[k]),
			Journal:       true,
			SecurityEvent: true,
		}
	}

	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.DisallowUnknownFields()
	var e Envelope
	if err := dec.Decode(&e); err != nil {
		msg := err.Error()
		if strings.Contains(msg, "unknown field") {
			return nil, &Refusal{
				Status: http.StatusBadRequest,
				Reason: ReasonUnknownField,
				Detail: msg,
			}
		}
		return nil, &Refusal{Status: http.StatusBadRequest, Reason: ReasonMalformed, Detail: msg}
	}
	// Trailing content. `{"intent":"x"} {"intent":"y"}` decodes the first object without error,
	// so a second envelope smuggled into the same body would be silently discarded -- and the
	// caller would have been told the request succeeded.
	if dec.More() {
		return nil, &Refusal{
			Status: http.StatusBadRequest,
			Reason: ReasonMalformed,
			Detail: "body contains more than one JSON value",
		}
	}

	if err := e.Validate(); err != nil {
		return nil, err
	}
	return &e, nil
}

// Validate enforces the 06 §4.1 field reference. It does not consult the cluster, the clock or
// the caller's identity -- those are steps 1 and 3 of the pipeline and live elsewhere.
func (e *Envelope) Validate() error {
	if e.APIVersion != APIVersion {
		return invalid("apiVersion must be %q, got %q", APIVersion, e.APIVersion)
	}
	if e.Kind != EnvelopeKind {
		return invalid("kind must be %q, got %q", EnvelopeKind, e.Kind)
	}

	switch n := len(e.Intent); {
	case n == 0:
		return invalid("intent is required")
	case n > MaxIntentLen:
		return invalid("intent is %d characters, the limit is %d", n, MaxIntentLen)
	}
	if strings.ContainsAny(e.Intent, "\n\r") {
		// It is rendered into a chat line, a digest row and the record. A newline there is a way
		// to make the second half of an intent invisible in one of the three.
		return invalid("intent must be a single line")
	}
	if len(e.Rationale) > MaxRationaleLen {
		return invalid("rationale is %d characters, the limit is %d", len(e.Rationale), MaxRationaleLen)
	}

	switch n := len(e.Operations); {
	case n == 0:
		return invalid("at least one operation is required")
	case n > MaxOperations:
		return invalid("%d operations, the limit is %d", n, MaxOperations)
	}
	for i := range e.Operations {
		if err := e.Operations[i].validate(i); err != nil {
			return err
		}
	}

	if !validRequesterKinds[e.Requester.Kind] {
		return invalid("requester.kind must be human, agent or system, got %q", e.Requester.Kind)
	}
	if !validPlatforms[e.Requester.Platform] {
		return invalid("requester.platform %q is not a known platform", e.Requester.Platform)
	}
	if e.Requester.Kind != "system" && e.Requester.ID == "" {
		// A `system` requester has no principal by definition. Anything else does, and an empty
		// id there would produce a record that cannot answer "who asked for this?".
		return invalid("requester.id is required for a %s requester", e.Requester.Kind)
	}
	if !validTriggerSources[e.Trigger.Source] {
		return invalid("trigger.source %q is not one of chat, watch, alert, cron, delegation, escalation, undo", e.Trigger.Source)
	}
	if !hex32Re.MatchString(e.Trace.TraceID) {
		return invalid("trace.traceId must be 32 lowercase hex characters (W3C trace-id)")
	}

	if e.IssuedAt == "" {
		return invalid("issuedAt is required")
	}
	if _, err := ParseIssuedAt(e.IssuedAt); err != nil {
		return invalid("issuedAt: %v", err)
	}
	if !hex32Re.MatchString(e.Nonce) {
		return invalid("nonce must be 32 lowercase hex characters")
	}
	if len(e.IdempotencyKey) > MaxIdempotencyKeyLen {
		return invalid("idempotencyKey is %d characters, the limit is %d", len(e.IdempotencyKey), MaxIdempotencyKeyLen)
	}
	if !sha256KeyRe.MatchString(e.IdempotencyKey) {
		return invalid("idempotencyKey must be sha256:<64 lowercase hex>")
	}

	if e.MaxObjects != nil && *e.MaxObjects < 1 {
		return invalid("maxObjects must be at least 1, got %d", *e.MaxObjects)
	}
	if d := e.DeadlineSeconds; d != nil && (*d < MinDeadlineSeconds || *d > MaxDeadlineSeconds) {
		return invalid("deadlineSeconds must be between %d and %d, got %d", MinDeadlineSeconds, MaxDeadlineSeconds, *d)
	}
	return nil
}

func (o *Operation) validate(i int) error {
	where := fmt.Sprintf("operations[%d]", i)

	if !validOps[o.Op] {
		return invalid("%s.op must be create, apply, patch, delete or scale, got %q", where, o.Op)
	}

	targets := 0
	for _, present := range []bool{o.Target != nil, o.TargetSelector != nil, o.CloudTarget != nil} {
		if present {
			targets++
		}
	}
	if targets != 1 {
		return invalid("%s must carry exactly one of target, targetSelector or cloudTarget (found %d)", where, targets)
	}

	switch {
	case o.Target != nil:
		if o.Target.Version == "" || o.Target.Kind == "" || o.Target.Name == "" {
			return invalid("%s.target needs version, kind and name", where)
		}
	case o.TargetSelector != nil:
		if o.Op == "create" {
			// There is nothing to select. Accepting it would mean guessing what to create and
			// how many times.
			return invalid("%s: targetSelector is not valid with op: create", where)
		}
		s := o.TargetSelector
		if s.Version == "" || s.Kind == "" {
			return invalid("%s.targetSelector needs version and kind", where)
		}
		if s.Namespace == "" {
			// A selector with no namespace is a cluster-wide fan-out wearing the shape of a
			// namespaced one. 06 §4.1: it never crosses a namespace boundary.
			return invalid("%s.targetSelector.namespace is required; a selector never crosses a namespace boundary", where)
		}
		if labelSelectorBad.MatchString(s.LabelSelector) {
			// The empty selector matches EVERYTHING of that kind in the namespace. If that is
			// genuinely wanted it can be spelled out; it will not be arrived at by omission.
			return invalid("%s.targetSelector.labelSelector is required and may not be empty", where)
		}
	case o.CloudTarget != nil:
		c := o.CloudTarget
		if c.Provider == "" || c.Service == "" || c.Resource == "" || c.Method == "" {
			return invalid("%s.cloudTarget needs provider, service, resource and method", where)
		}
	}

	payloads := 0
	for _, present := range []bool{o.DesiredState != nil, o.Patch != nil, o.Scale != nil} {
		if present {
			payloads++
		}
	}

	switch o.Op {
	case "create", "apply":
		if o.DesiredState == nil {
			return invalid("%s: op %s requires desiredState", where, o.Op)
		}
		if payloads != 1 {
			return invalid("%s: op %s takes desiredState only", where, o.Op)
		}
	case "patch":
		if o.Patch == nil {
			return invalid("%s: op patch requires patch", where)
		}
		if payloads != 1 {
			return invalid("%s: op patch takes patch only", where)
		}
		if !validPatchTypes[o.Patch.Type] {
			return invalid("%s.patch.type %q is not an accepted media type", where, o.Patch.Type)
		}
		if o.Patch.Body == nil {
			return invalid("%s.patch.body is required", where)
		}
		// A JSON Patch is an array of ops and the other two are objects. Mismatching them is
		// accepted by the API server for some shapes and produces a different mutation than the
		// caller wrote, which is the worst available outcome.
		_, isArray := o.Patch.Body.([]any)
		if (o.Patch.Type == "application/json-patch+json") != isArray {
			return invalid("%s.patch.body shape does not match patch.type %q", where, o.Patch.Type)
		}
	case "scale":
		if o.Scale == nil || o.Scale.Replicas == nil {
			return invalid("%s: op scale requires scale.replicas", where)
		}
		if payloads != 1 {
			return invalid("%s: op scale takes scale only", where)
		}
		if *o.Scale.Replicas < 0 {
			return invalid("%s.scale.replicas may not be negative", where)
		}
	case "delete":
		if payloads != 0 {
			return invalid("%s: op delete takes no desiredState, patch or scale", where)
		}
	}

	if o.Delete != nil {
		if o.Op != "delete" {
			return invalid("%s: delete options are only valid with op: delete", where)
		}
		if !validPropagation[o.Delete.PropagationPolicy] {
			return invalid("%s.delete.propagationPolicy %q is not Foreground, Background or Orphan", where, o.Delete.PropagationPolicy)
		}
		if g := o.Delete.GracePeriodSeconds; g != nil && *g < 0 {
			return invalid("%s.delete.gracePeriodSeconds may not be negative", where)
		}
	}
	return nil
}

// ParseIssuedAt reads the timestamp and insists it is UTC.
//
// RFC-3339 admits an offset, and `2026-07-24T13:58:01-04:00` is the same instant as the UTC form.
// It is refused anyway: the freshness window is computed by subtraction, so an offset would not
// break it, but every downstream reader -- the record, the export, the chat digest -- compares
// these strings, and two spellings of one instant that sort differently is a bug that only shows
// up in an incident timeline.
func ParseIssuedAt(s string) (time.Time, error) {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return time.Time{}, fmt.Errorf("not an RFC-3339 timestamp: %w", err)
	}
	if !strings.HasSuffix(s, "Z") {
		return time.Time{}, fmt.Errorf("must be UTC with a trailing Z, got %q", s)
	}
	return t.UTC(), nil
}

// EffectiveMaxObjects and EffectiveDeadline apply the documented defaults. They are methods rather
// than defaults filled in during decode so that the decoded envelope stays byte-faithful to what
// the caller sent -- which is what the idempotency key is recomputed from.
func (e *Envelope) EffectiveMaxObjects() int {
	if e.MaxObjects == nil {
		return DefaultMaxObjects
	}
	return *e.MaxObjects
}

func (e *Envelope) EffectiveDeadline() time.Duration {
	if e.DeadlineSeconds == nil {
		return DefaultDeadlineSeconds * time.Second
	}
	return time.Duration(*e.DeadlineSeconds) * time.Second
}
