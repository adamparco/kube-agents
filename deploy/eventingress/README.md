# eventingress — deferrable cloud-push relay (Phase 4 D1)

`eventingress` delivers **non-chat machine push** — alerts (Cloud Monitoring / Alertmanager over
Pub/Sub) and GitHub webhooks — to an agent's **local session-inject seam** (`127.0.0.1:8699`, hardened
in S1). It reuses the exact bearer + `X-Asserted-Caller` owner + kind-discriminated envelope the
`k8s-event-watcher` sidecar already speaks (04 §4), so every machine push terminates at **one in-pod
delivery contract**.

## The one delivery contract

Every source normalizes to a `{kind: ...}` event, then the relay does:

1. `POST /sessions` with `Authorization: Bearer <API_SERVER_KEY>` + `X-Asserted-Caller: <owner>` → `201 {"sessionID": "..."}`
2. `POST /sessions/{sid}/inject` with `{"message": "<inner-json>"}`, where the inner JSON carries `kind`

The seam routes on `kind` (S2). Recognized kinds: `alert`, `github`, `escalation`, `k8s-event`.

## Two source modes

| `--source`  | What it does                                                                             | Where |
| ----------- | ---------------------------------------------------------------------------------------- | ----- |
| `synthetic` | Read one already-normalized `{kind:...}` event from `--event-file`, deliver once, exit 0 | Kind  |
| `pubsub`    | Drain **pre-created** alert / GitHub subscriptions (subscribe-only, never publishes)     | GKE   |

Both modes go through the **same** `Relay` — the synthetic mode is not a fake, it just skips Pub/Sub.

## Invariants

- **Subscribe-only.** eventingress never creates a topic or subscription and never publishes. It only
  drains subscriptions provisioned out of band (Terraform / gcloud).
- **Read-only / GitOps-only writes.** eventingress delivers events; it never mutates cluster or cloud
  state. The only write path remains a reviewed GitOps PR.
- **No cross-tier network path (invariant 3).** Delivery is a same-pod loopback call to `127.0.0.1`;
  eventingress runs as a per-pod sidecar, never a shared service.

## Kind vs. scratch GKE

- **Kind (inner loop):** the in-pod delivery path is proven hermetically with the synthetic terminus —
  a `{kind:alert}` / `{kind:github}` file delivered through the real relay to the seam. No Pub/Sub. See
  `verify-phase4.sh`.
- **Scratch GKE (deferred, not faked):** the `pubsub` source drains real subscriptions. The cloud
  transport wiring (Pub/Sub topics/subscriptions, Cloud Monitoring notification channel, GitHub webhook
  with HMAC) is provisioned as part of scratch-GKE setup, then this relay consumes it. The subscribe
  path itself is exercised in unit tests against the in-process `pstest` emulator (same approach the
  router's Pub/Sub receiver uses), so the code is real even though the transport is deferred.

## Deploying on scratch GKE

The binary ships in the agent image (`deploy/docker/Dockerfile`) at `/usr/local/bin/eventingress`. It
is **not** injected by the operator (unlike the watcher), because it needs cloud subscriptions that
exist only on GKE. Attach it out-of-band with the strategic-merge patch in this directory:

```sh
kubectl patch deployment <agent-name> -n kubeagents-system \
  --type=strategic --patch-file deploy/eventingress/sidecar-patch.yaml
```

Replace the `PLACEHOLDER-*` values (agent name, image, tier/owner, GCP project, subscription IDs).

## Egress

The Pub/Sub subscribe leg resolves to the restricted Google APIs VIP, so it rides the **existing**
Private Google Access rule (`199.36.153.8/30:443`) in each tier's egress `NetworkPolicy` — no new CIDR
or port. The rule comment in those templates documents the subscribe-only allowance.
