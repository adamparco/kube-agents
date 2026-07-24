---
type: runbook
title: Runbook — workload CrashLoopBackOff
tags: [runbook, reliability, crashloop, sre]
resource: apps/v1/Deployment
timestamp: 2026-07-24T00:00:00Z
---

# Runbook — workload CrashLoopBackOff

Operational procedure for a workload stuck in `CrashLoopBackOff` (06 §5, `runbook`). This is the read
half of indirect coordination: an agent that sees a crash-looping pod — via a **Kubernetes watch**
(the fast path) or the **heartbeat backstop** (04 §4) — retrieves this runbook with the
[read-knowledge](../index.md) skill instead of re-deriving a fix, then proposes any correction as a
reviewed PR. **You are read-only** — every step below is a read-only `get`/`describe`/`logs`; never
`edit`/`patch`/`delete` a live object.

## Symptom

- `kubectl get pods -n <namespace>` shows a pod in `CrashLoopBackOff` with a rising restart count.
- A `BackOff`/`Failed` Event is emitted for the pod (this is what a namespace-scoped watch reacts to).

## Triage (read-only)

1. **Read the crash reason:**
   ```bash
   kubectl -n <namespace> describe pod <pod>            # Last State, Reason, Exit Code, Events
   kubectl -n <namespace> logs <pod> --previous         # logs from the crashed container
   ```
2. **Classify the exit** — the exit code narrows the cause:
   - **Exit 1 / application stack trace** → application bug or bad config/env value.
   - **Exit 137 (OOMKilled)** → memory limit too low, or a leak. Check `Last State: OOMKilled`.
   - **`CreateContainerConfigError` / `CreateContainerError`** → a missing `ConfigMap`/`Secret` key or
     a bad image entrypoint.
   - **`ImagePullBackOff`** (adjacent) → bad image ref or missing pull credentials.
3. **Confirm it is the workload, not the platform:** the node is `Ready` and not under pressure, and the
   crash is isolated to this workload (not a cluster-wide symptom that belongs to a higher tier).

## Correct (propose, never mutate)

Reconcile only through the GitOps loop — the fix is a reviewed PR against the workload's desired state,
never a direct `kubectl edit`:

- **OOMKilled** → propose raising the container `resources.limits.memory` (and matching `requests`) in
  the workload manifest.
- **Config/secret error** → propose the missing key or corrected env reference in the manifest / a
  referenced `ConfigMap`.
- **Application bug** → propose pinning back to the last-good image tag/digest while the owning team
  fixes forward.

Open the change with the `submit-suggestion` skill (in your tier's `*-agent/` branch namespace). If the
fix is **out of your scope** (e.g. a namespace agent finds a cluster-scoped cause), raise it with the
`raise-escalation` skill — never contact another agent directly (invariant 3).

## Related

- Baseline the workload's cluster runs on: [standard GKE cluster blueprint](../cluster-blueprint/standard-gke.md).
- Back to the [OKF index](../index.md).
