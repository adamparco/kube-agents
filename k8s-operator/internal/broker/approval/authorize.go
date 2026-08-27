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

package approval

import (
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// Decision is the gateway's authorization verdict for one command, before any write is attempted.
// A Decision never causes the caller to move phase past what it says: Allowed=false always means
// "make no write", never "write something weaker".
type Decision struct {
	// Allowed reports whether the command may proceed to a write.
	Allowed bool

	// Reason is populated on refusal, and is the text a human sees back in chat. It never repeats
	// a principal's own input or roster membership beyond what the requester could already infer —
	// this is a decision message, not a debugging dump of the roster.
	Reason string
}

func refuse(reason string) Decision { return Decision{Allowed: false, Reason: reason} }
func allow() Decision               { return Decision{Allowed: true} }

// Authorize is the one gate both approve and reject pass through: the record must be
// PendingApproval, the caller must be on the roster, and the approval window (if one is already
// running) must not have closed. It does not decide the verb-specific rules — four-eyes for
// approve, nothing extra for reject — those are AuthorizeApprove/AuthorizeReject below, so the
// shared checks cannot silently diverge between the two commands.
func Authorize(roster *agentv1alpha1.ApprovalRoster, ar *agentv1alpha1.ActionRecord, principal string, now time.Time) Decision {
	if ar == nil {
		return refuse("no such action")
	}
	// PendingApproval is the only phase either verb may act on. This is a courtesy check ahead of
	// the same rule the VAP enforces unconditionally (oldPhase == 'PendingApproval') — refusing
	// here means the gateway can say WHY in chat instead of relaying a bare admission denial.
	if ar.Status.Phase != agentv1alpha1.PhasePendingApproval {
		return refuse("this action is not awaiting approval (phase is " + string(ar.Status.Phase) + ")")
	}
	if roster == nil {
		return refuse("this action has no usable approval roster")
	}
	if !roster.HasApprover(principal) {
		return refuse("you are not on the approval roster for this action")
	}
	// Expiry is never an approval (chat-approval.md §4 sequence 3). The broker's resumption loop
	// owns moving the phase to Expired; this is the gateway's own belt-and-suspenders read of the
	// same deadline, so a command that arrives in the race window between the deadline passing and
	// the resumption loop's next reconcile is refused here rather than raced through. A late write
	// that gets past this check anyway still fails at admission, because by then the resumption
	// loop has moved the phase off PendingApproval and the VAP's oldPhase guard denies it.
	if ar.Status.Approvals != nil && ar.Status.Approvals.ExpiresAt != nil && !now.Before(ar.Status.Approvals.ExpiresAt.Time) {
		return refuse("the approval window for this action has expired")
	}
	return allow()
}

// AuthorizeApprove adds four-eyes to Authorize's shared checks: with AllowSelfApproval false, the
// requester may not approve their own action, and an AttributionUnverified requester still counts
// for the denial — 06 §1.2's rule is "deny on match, never allow on doubt" (chat-approval.md §5).
func AuthorizeApprove(roster *agentv1alpha1.ApprovalRoster, ar *agentv1alpha1.ActionRecord, principal string, now time.Time) Decision {
	if d := Authorize(roster, ar, principal, now); !d.Allowed {
		return d
	}
	if !roster.SelfApprovalAllowed() && SamePrincipal(principal, ar.Spec.Requester.ID) {
		return refuse("you requested this action; the roster requires a different approver (allowSelfApproval is false)")
	}
	return allow()
}

// AuthorizeReject is Authorize with no additional rule: any roster member may reject, including
// the requester — a reject only ever narrows what executes, so it carries none of approve's
// self-dealing risk (chat-approval.md §4 sequence 2).
func AuthorizeReject(roster *agentv1alpha1.ApprovalRoster, ar *agentv1alpha1.ActionRecord, principal string, now time.Time) Decision {
	return Authorize(roster, ar, principal, now)
}
