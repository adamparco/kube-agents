---
name: propose-action
description: >-
  Propose a Kubernetes cluster mutation over the A2A bus to the Action Broker
  using `a2a propose --file <envelope.json>` or `broker_client.py`. Mints an
  attenuated `hop.<session>.<seq>` capability bound to the envelope's RFC 8785
  JCS SHA-256 digest and waits for the broker's decision event.
metadata:
  category: Platform
---

# Propose Action: Capability-Attenuated Mutations via the Action Broker

Session and platform pods are strictly read-only against the Kubernetes API. When a task requires mutating cluster state (scaling a Deployment, applying a ConfigMap, patching a workload, or deleting a stuck Pod), you propose an `ActionEnvelope` to the **Action Broker** over the A2A bus.

## How Capability Attenuation Works

When your session was spawned, the gateway minted a root capability in the NATS KV `cap` bucket and injected its coordinates into your environment:
- `A2A_CAPABILITY_KEY` (`root.<taskId>`)
- `A2A_CAPABILITY_REVISION` (`<uint64>`)
- `A2A_AUTHORITY_TIER` (`developer-team` | `cluster-admin` | `platform`)
- `A2A_AUTHORITY_SCOPE` (`namespace/<ns>`)

When you run `a2a propose --file <envelope.json>`, the CLI:
1. Canonicalizes your `ActionEnvelope` JSON with RFC 8785 JCS and computes its SHA-256 digest (`envelopeSha256`).
2. Mints an attenuated child capability `hop.<session>.<seq>` in `KV_cap` with `delegate: "broker"` and `envelopeSha256: <digest>`.
3. Publishes the proposal to `a2a.broker.propose.<namespace>.<agent>` and waits for the broker's verdict on `a2a.broker.events.<namespace>.<actionId>`.

## Workflows

### 1. Propose Using `broker_client.py` (Recommended)

`broker_client.py` constructs the JCS-canonicalized `ActionEnvelope`, computes the required `idempotencyKey`, and invokes `a2a propose` automatically when `A2A_NATS_URL` / `NATS_URL` is set:

```bash
python3 /opt/scripts/broker_client.py propose \
  --op scale \
  --api-version apps/v1 \
  --kind Deployment \
  --namespace "$A2A_NAMESPACE" \
  --name web \
  --replicas 3 \
  --intent "Scale Deployment/web to 3 replicas"
```

### 2. Propose an Explicit `ActionEnvelope` File via `a2a propose`

If you already have a complete `ActionEnvelope` JSON file (with a valid `idempotencyKey`), pipe it directly to `a2a propose`:

```bash
a2a propose --file /tmp/envelope.json \
  --namespace "$A2A_NAMESPACE" \
  --agent "$A2A_AGENT_NAME"
```

## Handling the Broker Response

The command prints the broker's `ActionEventMessage` JSON to `stdout`:

- **`"decision": "accepted"`, `"phase": "Verified"`**: The mutation executed and passed post-condition verification.
- **`"decision": "gated"`, `"phase": "PendingApproval"`**: The broker parked the `ActionRecord` awaiting human approval in chat (`/approve <actionId>` or `/reject <actionId> <reason>`). Report the `actionId` and stop; do not retry the proposal.
- **`"decision": "refused"`**: The broker or capability verifier rejected the proposal. Relay the `reason` and `message` clearly to the caller.

## Inspecting Live Action Status & Out-of-Band ChatOps Commands

Human ChatOps commands (`/approve <actionId>`, `/reject <actionId> <reason>`, `/undo <actionId> [reason]`, `/uncontest <actionId>`) are routed out-of-band by the gateway directly to `chatops-gateway` and mutate `ActionRecord` / `UndoRequest` custom resources in Kubernetes without appearing in your chat transcript. Whenever a user asks about an action, approval, undo, or contested resource, **always query the live Kubernetes state first**:

```bash
python3 /opt/scripts/broker_client.py --op status --namespace "$A2A_NAMESPACE" --action-id <actionId>
python3 /opt/scripts/broker_client.py --op list --namespace "$A2A_NAMESPACE" --limit 10
```
