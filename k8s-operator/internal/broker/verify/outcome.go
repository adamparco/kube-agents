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

package verify

import (
	"fmt"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Cause is one of the nine failure causes 04 §5.1 names, plus the honest tenth.
//
// The list is closed on purpose. "Transient vs terminal" is the decision that picks between waiting
// and rolling back, and a free-text reason would make it a matter of whoever wrote the log line.
type Cause string

const (
	// --- transient (04 §5.1: "conflicts, throttling, a dependency still converging, a scheduler
	// waiting on capacity that is arriving") ---

	// CauseConflict is an optimistic-concurrency conflict: someone else wrote first.
	CauseConflict Cause = "Conflict"
	// CauseThrottled is server-side rate limiting.
	CauseThrottled Cause = "Throttled"
	// CauseDependencyConverging is a prerequisite object that has not finished reconciling.
	CauseDependencyConverging Cause = "DependencyConverging"
	// CauseCapacityArriving is a scheduler waiting on capacity that a signal says is on its way.
	CauseCapacityArriving Cause = "CapacityArriving"

	// --- terminal (04 §5.1: "schema or policy rejection, admission denial, quota exhaustion with
	// no pending capacity, a nonexistent image, verification still failing at the end of the settle
	// window") ---

	// CauseSchemaRejected is a validation failure -- the object is not well-formed for its schema.
	CauseSchemaRejected Cause = "SchemaRejected"
	// CauseAdmissionDenied is a webhook or policy denial.
	CauseAdmissionDenied Cause = "AdmissionDenied"
	// CauseQuotaExhausted is quota exhaustion with no pending capacity.
	CauseQuotaExhausted Cause = "QuotaExhausted"
	// CauseImageMissing is an image reference that does not resolve.
	CauseImageMissing Cause = "ImageMissing"
	// CauseSettleWindowExpired is verification still failing when the window closed. It is the
	// BACKSTOP: every wait ends here, so no transient classification can wait forever.
	CauseSettleWindowExpired Cause = "SettleWindowExpired"

	// CauseUnknown is a failure that matched none of the nine. See DispositionOf for why it waits
	// rather than rolls back.
	CauseUnknown Cause = "Unknown"
)

// Disposition is what the cause implies for the recovery ladder.
type Disposition string

const (
	// Transient goes to rung 1 (retry with backoff).
	Transient Disposition = "Transient"
	// Terminal triggers rung 3 automatically: replay the undo plan, mark RolledBack, report.
	Terminal Disposition = "Terminal"
)

// namedCauses is the 04 §5.1 table, transcribed once. `terminalCauses` and `transientCauses` are
// derived from it rather than being a second list, because two lists of nine drift.
var namedCauses = map[Cause]Disposition{
	CauseConflict:             Transient,
	CauseThrottled:            Transient,
	CauseDependencyConverging: Transient,
	CauseCapacityArriving:     Transient,

	CauseSchemaRejected:      Terminal,
	CauseAdmissionDenied:     Terminal,
	CauseQuotaExhausted:      Terminal,
	CauseImageMissing:        Terminal,
	CauseSettleWindowExpired: Terminal,
}

// NamedCauses returns the nine causes 04 §5.1 enumerates, so a test can assert the table is
// complete without retyping it. CauseUnknown is deliberately absent: it is not one of the nine.
func NamedCauses() []Cause {
	out := make([]Cause, 0, len(namedCauses))
	for c := range namedCauses {
		out = append(out, c)
	}
	return out
}

// DispositionOf maps a cause to its disposition.
//
// CauseUnknown is TRANSIENT, and the direction is a deliberate choice rather than a default that
// fell out of a switch. Terminal means "take a second, unreviewed, mutating action right now" --
// the automatic rollback -- on the strength of an error nobody parsed. Transient means "keep
// looking", and it cannot run forever because CauseSettleWindowExpired is itself terminal: an
// unrecognized failure that persists is rolled back when the window closes, by the one cause whose
// whole job is to be the floor under every wait.
func DispositionOf(c Cause) Disposition {
	if d, ok := namedCauses[c]; ok {
		return d
	}
	return Transient
}

// CapacitySignal is the discriminator between the transient "a scheduler waiting on capacity that
// is arriving" and the terminal "quota exhaustion with no pending capacity" (04 §5.1).
//
// It is an INPUT rather than something this package infers, because 09 §12 row T-10 records that
// the spec names no signal for it -- and 09 §12 is explicit that a harness may not pick its own
// number for an unresolved tightening. The three-valued type is the honest shape: a caller that has
// not looked says so, and Unknown is not silently folded into either answer.
type CapacitySignal string

const (
	// CapacityArriving means a pending scale operation or autoscaler event was observed.
	CapacityArriving CapacitySignal = "Arriving"
	// CapacityExhausted means the caller looked and found no pending capacity.
	CapacityExhausted CapacitySignal = "Exhausted"
	// CapacityUnknown means the caller did not look, or could not.
	CapacityUnknown CapacitySignal = "Unknown"
)

// Failure is everything the classifier is allowed to read. Prose is not on the list: `Message` is
// the API server's own text, matched against server-generated substrings, never an agent's
// rationale.
type Failure struct {
	// Err is the API error, when the failure came from a call.
	Err error
	// Message is a status or event message when there was no error object -- e.g. a pod's
	// scheduling condition, or a container waiting reason.
	Message string
	// WaitingReason is a container's `state.waiting.reason`, e.g. ErrImagePull, if one was observed.
	WaitingReason string
	// Capacity is the T-10 discriminator. Only consulted for a pending-on-capacity failure.
	Capacity CapacitySignal
	// SettleWindowExpired is set by the driver when the window closed with the predicate still
	// failing. It wins over every other signal, because 04 §5.1 makes it terminal by itself.
	SettleWindowExpired bool
}

// imagePullReasons are the container waiting reasons that mean the image does not resolve.
// `ImagePullBackOff` is included: it is the backoff for a pull that keeps failing, and treating it
// as transient is how a typo'd tag waits out its whole settle window before anyone learns anything.
var imagePullReasons = map[string]bool{
	"ErrImagePull":         true,
	"ImagePullBackOff":     true,
	"InvalidImageName":     true,
	"ImageInspectError":    true,
	"RegistryUnavailable":  false, // genuinely transient -- the registry, not the reference.
	"ErrImageNeverPull":    true,
	"CreateContainerError": false,
}

// CauseOf derives the cause of a failure. It reads structured signals first and message text only
// where Kubernetes offers nothing else.
func CauseOf(f Failure) Cause {
	// The backstop first: 04 §5.1 makes an expired window terminal regardless of why it expired,
	// and checking it last would let a transient-looking cause outrank it.
	if f.SettleWindowExpired {
		return CauseSettleWindowExpired
	}

	if f.WaitingReason != "" {
		if terminal, known := imagePullReasons[f.WaitingReason]; known {
			if terminal {
				return CauseImageMissing
			}
			return CauseDependencyConverging
		}
	}

	if f.Err != nil {
		switch {
		case apierrors.IsConflict(f.Err):
			return CauseConflict
		case apierrors.IsTooManyRequests(f.Err), apierrors.IsServerTimeout(f.Err), apierrors.IsTimeout(f.Err):
			return CauseThrottled
		case apierrors.IsInvalid(f.Err), apierrors.IsBadRequest(f.Err), apierrors.IsUnsupportedMediaType(f.Err):
			return CauseSchemaRejected
		case isAdmissionDenial(f.Err):
			return CauseAdmissionDenied
		case isQuotaError(f.Err):
			return capacityCause(f.Capacity)
		}
	}

	msg := strings.ToLower(f.Message)
	switch {
	case msg == "":
		return CauseUnknown
	case strings.Contains(msg, "insufficient"), strings.Contains(msg, "exceeded quota"),
		strings.Contains(msg, "didn't have free ports"), strings.Contains(msg, "nodes are available"):
		// "0/3 nodes are available: 3 Insufficient cpu." -- the scheduler cannot place the pod. Which
		// of the two 04 §5.1 rows this is depends entirely on the capacity signal.
		return capacityCause(f.Capacity)
	case strings.Contains(msg, "denied the request"), strings.Contains(msg, "admission webhook"):
		return CauseAdmissionDenied
	case strings.Contains(msg, "is invalid"), strings.Contains(msg, "validation failure"):
		return CauseSchemaRejected
	}
	return CauseUnknown
}

// capacityCause resolves the one 04 §5.1 distinction the spec does not give a signal for (T-10).
//
// Unknown resolves to CauseDependencyConverging -- transient -- for the same reason CauseUnknown
// does: waiting is bounded by the settle window, rolling back is not bounded by anything. What it
// must NOT do is silently answer "exhausted", which would make an unresolved spec row into an
// automatic rollback that nobody chose.
func capacityCause(sig CapacitySignal) Cause {
	switch sig {
	case CapacityArriving:
		return CauseCapacityArriving
	case CapacityExhausted:
		return CauseQuotaExhausted
	default:
		return CauseDependencyConverging
	}
}

func isAdmissionDenial(err error) bool {
	if !apierrors.IsForbidden(err) {
		return false
	}
	// A plain RBAC "forbidden" and a webhook denial are both 403. The webhook path names itself.
	return strings.Contains(strings.ToLower(err.Error()), "admission") ||
		strings.Contains(strings.ToLower(err.Error()), "denied the request") ||
		reasonIs(err, metav1.StatusReasonForbidden) && strings.Contains(strings.ToLower(err.Error()), "policy")
}

func isQuotaError(err error) bool {
	if apierrors.IsResourceExpired(err) {
		return false
	}
	low := strings.ToLower(err.Error())
	return apierrors.IsForbidden(err) && (strings.Contains(low, "exceeded quota") ||
		strings.Contains(low, "insufficient quota"))
}

func reasonIs(err error, want metav1.StatusReason) bool {
	return apierrors.ReasonForError(err) == want
}

// String makes a Cause printable in the one place it reaches a human: the recovery reason.
func (c Cause) String() string { return string(c) }

// Describe renders the cause and its disposition for a ladder transition reason.
func Describe(c Cause) string {
	return fmt.Sprintf("%s (%s)", c, DispositionOf(c))
}
