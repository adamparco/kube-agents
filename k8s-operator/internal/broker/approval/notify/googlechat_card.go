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

package notify

import (
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// GoogleChatCard renders a Card v2 message body for the Google Chat REST API's
// spaces.messages.create/patch. Buttons are the same non-authoritative convenience as the Slack
// rendering — a click still resolves the clicking principal from the platform-verified payload and
// runs the ordinary approve/reject authorization path (05 §1.8).
func GoogleChatCard(m approval.Message) map[string]any {
	if m.Resolution != "" {
		return map[string]any{"text": strings.ToUpper(m.Resolution) + ": " + m.Text()}
	}

	widgets := []any{
		textParagraph(fmt.Sprintf("<b>%s</b> is asking to: %s", m.RequesterID, m.Intent)),
		decoratedText("Risk class", string(m.Class)),
		decoratedText("Approvals", fmt.Sprintf("%d/%d", len(m.Granted), m.Required)),
	}
	if !m.ExpiresAt.IsZero() {
		widgets = append(widgets, decoratedText("Expires", m.ExpiresAt.UTC().Format("2006-01-02T15:04:05Z")))
	}
	if len(m.Reasons) > 0 {
		widgets = append(widgets, textParagraph("<b>Why:</b><br>"+strings.Join(m.Reasons, "<br>")))
	}
	if len(m.Targets) > 0 {
		widgets = append(widgets, textParagraph("<b>Targets:</b><br>"+strings.Join(m.Targets, "<br>")))
	}
	widgets = append(widgets, map[string]any{
		"buttonList": map[string]any{
			"buttons": []any{
				chatButton("Approve", "approve "+m.RecordName),
				chatButton("Reject", "reject "+m.RecordName),
			},
		},
	})
	widgets = append(widgets, textParagraph(fmt.Sprintf(
		"Or reply with <code>approve %s</code> or <code>reject %s &lt;reason&gt;</code>", m.RecordName, m.RecordName)))

	return map[string]any{
		"text": m.Text(),
		"cardsV2": []any{
			map[string]any{
				"cardId": "kage-approval-" + m.RecordName,
				"card": map[string]any{
					"header": map[string]any{"title": "Action gated: " + m.RecordName},
					"sections": []any{
						map[string]any{"widgets": widgets},
					},
				},
			},
		},
	}
}

func textParagraph(text string) map[string]any {
	return map[string]any{"textParagraph": map[string]any{"text": text}}
}

func decoratedText(label, text string) map[string]any {
	return map[string]any{"decoratedText": map[string]any{"topLabel": label, "text": text}}
}

func chatButton(label, command string) map[string]any {
	return map[string]any{
		"text": label,
		"onClick": map[string]any{
			"action": map[string]any{
				"function": "kageCommand",
				"parameters": []any{
					map[string]any{"key": "command", "value": command},
				},
			},
		},
	}
}
