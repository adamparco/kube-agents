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

import "fmt"

// SlackPrincipal renders a Slack member ID in the canonical 06 §1.2 V-11 form. The caller passes
// the raw platform ID from the verified event payload (e.g. "U02ABCDEF") — never a display name,
// a handle, or an email; see Approver.ID's own doc comment for why those are excluded.
func SlackPrincipal(userID string) string {
	return fmt.Sprintf("slack:%s", userID)
}

// GoogleChatPrincipal renders a Google Chat user resource name in the canonical form.
func GoogleChatPrincipal(userName string) string {
	return fmt.Sprintf("googlechat:%s", userName)
}

// SamePrincipal compares two canonical "<platform>:<id>" principals for equality.
//
// Exact string equality and nothing else — no case-folding, no whitespace trimming. A roster
// principal and a requester ID are both written by code that already canonicalizes on the way in
// (Approver.Principal(), the gateway's Slack/GoogleChatPrincipal), so a mismatch here is either two
// genuinely different principals or a canonicalization bug upstream, and normalizing the comparison
// would hide the second case as a false match on the first.
func SamePrincipal(a, b string) bool {
	return a != "" && a == b
}
