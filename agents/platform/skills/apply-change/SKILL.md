---
name: apply-change
description: >-
  Propose and execute Kubernetes cluster mutations (scale, apply, create,
  patch, delete) through the Action Broker using `/opt/scripts/broker_client.py`.
  Use whenever you need to mutate a Kubernetes resource or when `kubectl`
  returns a `read_only_refusal` escalation hint.
---

# Apply Change via the Action Broker

Direct mutating `kubectl` commands (`apply`, `patch`, `scale`, `delete`, `create`) are blocked by the read-only credential proxy (`403 read_only_refusal`). All cluster mutations must be submitted as signed `ActionEnvelope` proposals to the **Action Broker**, which classifies risk, generates a dry-run-validated undo plan, snapshots pre-state, gates high-risk changes on human approval, and verifies post-conditions.

## Quick CLI Usage (`broker_client.py`)

### 1. Scale a Workload
```bash
python3 /opt/scripts/broker_client.py \
  --op scale \
  --api-version apps/v1 \
  --kind Deployment \
  --namespace <namespace> \
  --name <deployment-name> \
  --replicas <count> \
  --intent "Scale Deployment/<deployment-name> to <count> replicas"
```

### 2. Apply or Create a Manifest
Write the desired Kubernetes resource as JSON to `/tmp/desired.json`, then submit:
```bash
python3 /opt/scripts/broker_client.py \
  --op apply \
  --api-version <apiVersion> \
  --kind <Kind> \
  --namespace <namespace> \
  --name <name> \
  --desired-file /tmp/desired.json \
  --intent "Apply <Kind>/<name> in <namespace>"
```

### 3. Patch a Resource
```bash
python3 /opt/scripts/broker_client.py \
  --op patch \
  --api-version apps/v1 \
  --kind Deployment \
  --namespace <namespace> \
  --name <name> \
  --patch-type application/merge-patch+json \
  --patch '{"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":"2026-10-09T00:00:00Z"}}}}}' \
  --intent "Rollout restart Deployment/<name>"
```

### 4. Delete a Resource
```bash
python3 /opt/scripts/broker_client.py \
  --op delete \
  --api-version v1 \
  --kind Pod \
  --namespace <namespace> \
  --name <pod-name> \
  --intent "Delete stuck Pod/<pod-name>"
```

### 5. Inspect Action Status or List Recent Actions (Authoritative Journal)
ChatOps slash commands (`/approve`, `/reject`, `/undo`, `/uncontest`) are handled out-of-band by the ChatOps gateway directly against Kubernetes `ActionRecord` and `UndoRequest` custom resources and do **not** appear in your chat transcript. **Never rely on earlier chat messages to determine whether an action was approved, executed, undone, or contested.** Always query the live broker journal first:

```bash
# Inspect a specific ActionRecord (and any associated UndoRequests):
python3 /opt/scripts/broker_client.py \
  --op status \
  --namespace <namespace> \
  --action-id <actionId>

# List the most recent ActionRecords in a namespace:
python3 /opt/scripts/broker_client.py \
  --op list \
  --namespace <namespace> \
  --limit 10
```

## Interpreting Broker Decisions & Lifecycle Phases

`broker_client.py` prints a JSON verdict to `stdout`:

- **`"decision": "accepted"`, `"phase": "Verified"`**: The broker classified the change as `routine` or `elevated`, captured a pre-state snapshot, applied the change via Server-Side Apply, and verified convergence. Relay the `actionId` and `report` fields (`noticed`, `did`, `verified`, `undo`) to the user.
- **`"decision": "gated"`, `"phase": "PendingApproval"`**: The change requires human approval from the `ApprovalRoster` before execution. Report the `actionId` (`recordName`) to the user and tell them an approver can run `/approve <actionId>` (or `/reject <actionId> <reason>`) in chat.
- **`"decision": "refused"`**: The broker refused the change (e.g., `scope-frozen`, `target-contested`, `agent-paused`, `blast-radius-cap-exceeded`). Report the `reason` and `message` to the user and do not attempt to bypass the broker. If `reason` is `target-contested`, inspect `broker_client.py --op list` to find the `Undone` action holding `contested: true` and tell the user they can clear it with `/uncontest <actionId>`.

## ChatOps Slash Commands Reference (User-Executed in Chat)

Users and approvers interact with `ActionRecord`s in chat using these exact slash commands (intercepted by `a2a-gateway` -> `chatops-gateway`):
- `/approve <actionId>` — Grants approval on a `PendingApproval` action, triggers execution + post-condition verification, and returns the verified result.
- `/reject <actionId> <reason>` — Rejects a `PendingApproval` action (`<reason>` is required).
- `/undo <actionId> [optional reason]` — Creates an `UndoRequest` to roll back a `Verified` action to its captured `preState`, marks the target resource as `contested: true` so agents will not immediately re-apply the reverted change, and returns the rollback result.
- `/uncontest <actionId>` — Clears `status.contested = false` on an `Undone` `ActionRecord` so future agent mutations on that target resource are permitted again.
