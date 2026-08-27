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

// Package notify is the approval notifier: it watches ActionRecords in phase PendingApproval and
// delivers, refreshes, and resolves chat notifications (docs/designs/broker/chat-approval.md §2).
// It never writes ActionRecord status — that is the ChatOps gateway's exclusive surface — so
// everything here is read-and-deliver.
package notify

import (
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// SlackBlocks renders a Block Kit message. Buttons are included as a later convenience only —
// clicking one still round-trips through the same typed-command authorization path as a human
// typing "approve <id>" (05 §1.8: buttons are never the authority), so the block payload carries
// no capability a plain-text reply does not also have.
func SlackBlocks(m approval.Message) map[string]any {
	if m.Resolution != "" {
		return map[string]any{
			"text": m.Text(),
			"blocks": []any{
				sectionBlock(fmt.Sprintf("*%s*\n%s", strings.ToUpper(m.Resolution), m.Text())),
			},
		}
	}

	fields := []any{
		mrkdwnField(fmt.Sprintf("*Requester*\n%s", m.RequesterID)),
		mrkdwnField(fmt.Sprintf("*Risk class*\n%s", m.Class)),
		mrkdwnField(fmt.Sprintf("*Approvals*\n%d/%d", len(m.Granted), m.Required)),
	}
	if !m.ExpiresAt.IsZero() {
		fields = append(fields, mrkdwnField(fmt.Sprintf("*Expires*\n%s", m.ExpiresAt.UTC().Format("2006-01-02T15:04:05Z"))))
	}

	blocks := []any{
		sectionBlock(fmt.Sprintf("*%s* is asking to: %s", m.RequesterID, m.Intent)),
		map[string]any{"type": "section", "fields": fields},
	}
	if len(m.Reasons) > 0 {
		blocks = append(blocks, sectionBlock("*Why:*\n"+bulletList(m.Reasons)))
	}
	if len(m.Targets) > 0 {
		blocks = append(blocks, sectionBlock("*Targets:*\n"+bulletList(m.Targets)))
	}
	blocks = append(blocks,
		map[string]any{"type": "divider"},
		map[string]any{
			"type": "actions",
			"elements": []any{
				buttonBlock("Approve", "approve", m.RecordName, "primary"),
				buttonBlock("Reject", "reject", m.RecordName, "danger"),
			},
		},
		sectionBlock(fmt.Sprintf("_Or reply with_ `approve %s` _or_ `reject %s <reason>`", m.RecordName, m.RecordName)),
	)

	return map[string]any{
		"text":   m.Text(), // required fallback for surfaces/notifications that cannot render blocks
		"blocks": blocks,
	}
}

func sectionBlock(text string) map[string]any {
	return map[string]any{
		"type": "section",
		"text": map[string]any{"type": "mrkdwn", "text": text},
	}
}

func mrkdwnField(text string) map[string]any {
	return map[string]any{"type": "mrkdwn", "text": text}
}

func buttonBlock(label, verb, recordName, style string) map[string]any {
	return map[string]any{
		"type":      "button",
		"text":      map[string]any{"type": "plain_text", "text": label},
		"action_id": "kage_" + verb,
		// value carries only the record name the payload's own claimed principal already has to be
		// re-resolved against — the click is a convenience for TYPING the command, not a bypass of
		// re-resolving who clicked (05 §1.8).
		"value": verb + " " + recordName,
		"style": style,
	}
}

func bulletList(items []string) string {
	var b strings.Builder
	for _, it := range items {
		b.WriteString("• ")
		b.WriteString(it)
		b.WriteString("\n")
	}
	return strings.TrimRight(b.String(), "\n")
}
