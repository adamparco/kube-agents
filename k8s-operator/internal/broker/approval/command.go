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
	"fmt"
	"strings"
)

// Verb is a v1 chat command. resume and uncontest are v2 (chat-approval.md §3) and have no Verb
// constant here on purpose — the parser must not recognize a v2 verb it cannot authorize.
type Verb string

const (
	VerbApprove Verb = "approve"
	VerbReject  Verb = "reject"
)

// Command is a parsed, not-yet-authorized chat command.
type Command struct {
	Verb     Verb
	ActionID string
	Reason   string
}

// ParseCommand parses the v1 typed-verb grammar: "approve <action-id>" and
// "reject <action-id> [reason...]" (chat-approval.md §3). The grammar is deliberately small: no
// aliases, no case-insensitive verbs, no leading slash (the platform adapters strip their own
// slash-command or app-mention prefix before calling this). A grammar a human can misread is a
// grammar that approves the wrong thing, so an unrecognized verb is a parse error, not a guess.
//
// The action ID is accepted as either the bare ULID (spec.actionId, e.g. "01ARZ3NDEKTSV4RRFFQ69G5FAV")
// or the record name form ("ar-01arz3ndektsv4rrffq69g5fav") — a human copies whichever one the
// notifier's message showed, and the message shows the record name (journal.RecordName's "ar-"
// form). The caller resolves either form the same way; see ActionRecordName.
func ParseCommand(text string) (Command, error) {
	fields := strings.Fields(strings.TrimSpace(text))
	if len(fields) < 2 {
		return Command{}, fmt.Errorf("approval: %q is not a recognized command; want \"approve <action-id>\" or \"reject <action-id> [reason]\"", text)
	}

	verb := Verb(fields[0])
	switch verb {
	case VerbApprove:
		if len(fields) > 2 {
			return Command{}, fmt.Errorf("approval: approve takes no reason; got %q", text)
		}
		return Command{Verb: VerbApprove, ActionID: fields[1]}, nil
	case VerbReject:
		return Command{
			Verb:     VerbReject,
			ActionID: fields[1],
			Reason:   strings.Join(fields[2:], " "),
		}, nil
	case "resume", "uncontest":
		return Command{}, fmt.Errorf("approval: %q is a v2 command; v1 supports only approve and reject", fields[0])
	default:
		return Command{}, fmt.Errorf("approval: unrecognized verb %q; want \"approve\" or \"reject\"", fields[0])
	}
}

// ActionRecordName normalizes either an action ID or a record name to the record name form
// ("ar-<lowercase ulid>"), the form a get-by-name lookup needs. It does not validate the ULID
// shape — that is the CRD schema's job (actionrecord_types.go's Pattern marker) — it only decides
// whether the "ar-" prefix needs adding.
func ActionRecordName(idOrName string) string {
	lower := strings.ToLower(idOrName)
	if strings.HasPrefix(lower, "ar-") {
		return lower
	}
	return "ar-" + lower
}
