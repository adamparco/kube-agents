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
	"context"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// DeliveryState is what the notifier remembers about one record's delivery, so a restart does not
// re-post a message that already exists and does not re-edit one that already shows the current
// state (chat-approval.md §2: "deliveries are deduplicated by an idempotence key").
type DeliveryState struct {
	Platform Platform `json:"platform"`
	Channel  string   `json:"channel"`
	// Ref is the platform message reference Deliver returned (Slack ts, or a Google Chat message
	// resource name), the handle Update addresses.
	Ref string `json:"ref"`
	// Key is the idempotence key of the LAST content actually delivered — record UID plus a
	// generation of phase and approval counts (chat-approval.md §2). A reconcile whose current key
	// matches this one is a no-op; the delivery already shows this exact state.
	Key string `json:"key"`
}

// Store persists DeliveryState per ActionRecord name. Delivery bookkeeping deliberately does not
// live on the ActionRecord: the notifier's RBAC is get/list/watch only (chat-approval.md §2), so
// annotating the record would require a status/spec write grant delivery does not otherwise need.
type Store interface {
	Get(ctx context.Context, recordName string) (DeliveryState, bool, error)
	Save(ctx context.Context, recordName string, state DeliveryState) error
}

// ConfigMapStore is the production Store: one ConfigMap in the notifier's own namespace, one data
// key per record name, JSON-encoded. A single object rather than one ConfigMap per record because
// PendingApproval records are, by definition, few and short-lived — the fleet's whole approval
// backlog fits comfortably under the 1 MiB ConfigMap limit long before it fits in a chat channel's
// patience.
type ConfigMapStore struct {
	Client    client.Client
	Name      string
	Namespace string
}

func (c *ConfigMapStore) Get(ctx context.Context, recordName string) (DeliveryState, bool, error) {
	cm := &corev1.ConfigMap{}
	key := client.ObjectKey{Name: c.Name, Namespace: c.Namespace}
	if err := c.Client.Get(ctx, key, cm); err != nil {
		if apierrors.IsNotFound(err) {
			return DeliveryState{}, false, nil
		}
		return DeliveryState{}, false, fmt.Errorf("notify: reading delivery state configmap %s: %w", key, err)
	}
	raw, ok := cm.Data[recordName]
	if !ok {
		return DeliveryState{}, false, nil
	}
	var state DeliveryState
	if err := json.Unmarshal([]byte(raw), &state); err != nil {
		return DeliveryState{}, false, fmt.Errorf("notify: decoding delivery state for %s: %w", recordName, err)
	}
	return state, true, nil
}

// Save writes one record's delivery state, retrying on a resourceVersion conflict — the reconciler
// may run more than one worker, and two workers touching different records both patch the same
// ConfigMap object.
func (c *ConfigMapStore) Save(ctx context.Context, recordName string, state DeliveryState) error {
	raw, err := json.Marshal(state)
	if err != nil {
		return fmt.Errorf("notify: marshaling delivery state for %s: %w", recordName, err)
	}

	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		cm := &corev1.ConfigMap{}
		key := client.ObjectKey{Name: c.Name, Namespace: c.Namespace}
		err := c.Client.Get(ctx, key, cm)
		switch {
		case apierrors.IsNotFound(err):
			cm = &corev1.ConfigMap{
				ObjectMeta: metav1.ObjectMeta{Name: c.Name, Namespace: c.Namespace},
				Data:       map[string]string{recordName: string(raw)},
			}
			return c.Client.Create(ctx, cm)
		case err != nil:
			return fmt.Errorf("notify: reading delivery state configmap %s: %w", key, err)
		}
		if cm.Data == nil {
			cm.Data = map[string]string{}
		}
		cm.Data[recordName] = string(raw)
		return c.Client.Update(ctx, cm)
	})
}

var _ Store = (*ConfigMapStore)(nil)
