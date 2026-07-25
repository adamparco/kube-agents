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

package router

import "strings"

// Authorize decides whether sender may reach target, BEFORE any dispatch (06 §2b; 03 §4a; Phase 2
// acceptance d). It is the central pre-dispatch enforcement point that fronts the per-tier pods.
//
// Fail-closed, by three rules that never fall through to "allow":
//
//  1. Empty sender ⇒ deny. An unauthenticated/unidentified requester is never authorized.
//  2. Empty or absent allowlist ⇒ deny ALL. Authorize reads ONLY the target CR's AllowedUsers and
//     consults no environment flag of any kind, so no pod-level configuration can widen it. This rule
//     was the only layer that already behaved correctly while the operator still rendered
//     GOOGLE_CHAT_ALLOW_ALL_USERS=true for an empty allowlist; P8-T1 deleted that backstop (V-CTR-014)
//     and every layer now agrees. A closed allowlist must be set explicitly to reach an agent.
//  3. Sender ∈ allowlist ⇒ allow; otherwise deny.
//
// Routing is never an authz signal: Authorize depends only on (target, sender), not on how the target
// was resolved (slash/handle/inference), so a cleverly-worded message cannot widen access.
func Authorize(target Target, sender string) Decision {
	sender = strings.TrimSpace(sender)
	if sender == "" {
		return Decision{Allowed: false, Reason: "empty sender: unidentified requester is never authorized"}
	}

	allowed := nonEmptyUsers(target.AllowedUsers)
	if len(allowed) == 0 {
		// Fail-closed: NOT the pod-env ALLOW_ALL default. An unset allowlist reaches no one via the router.
		return Decision{
			Allowed: false,
			Reason:  "closed allowlist: target " + target.Handle + " has no allowedUsers; router refuses all (fail-closed)",
		}
	}
	for _, u := range allowed {
		if u == sender {
			return Decision{Allowed: true, Reason: "sender in allowlist for " + target.Handle}
		}
	}
	return Decision{Allowed: false, Reason: "sender not in allowlist for " + target.Handle}
}

// nonEmptyUsers returns the trimmed, non-empty entries of the CR's AllowedUsers. It mirrors the
// operator's own empty-detection (a lone "" entry counts as empty) so the router and the pod agree on
// what "no allowlist" means — but the router's conclusion is the opposite one: refuse, not admit.
func nonEmptyUsers(users []string) []string {
	out := make([]string, 0, len(users))
	for _, u := range users {
		if u = strings.TrimSpace(u); u != "" {
			out = append(out, u)
		}
	}
	return out
}
