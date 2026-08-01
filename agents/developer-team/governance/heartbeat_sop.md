# SOP: Heartbeat Sweep (Backstop — Namespace Scope)

**Purpose:** A periodic **backstop** sweep of **your one assigned namespace** that catches drift or
degradation **no event trigger and no scheduled cron already covered**, and bounds worst-case detection
latency. This is the **last resort**, not the primary mechanism (04 §4): event triggers (a namespace-
scoped Kubernetes watch, alerts, GitHub webhooks) are the fast reactive path, cron handles genuinely
scheduled audits, and the heartbeat only sweeps for what slipped past both. A poll lags a fast-moving
problem and burns cycles when nothing changed — so keep it light and rely on it only as the safety net.

Detection here is **read-only**; remediation is not deferred. What this sweep finds inside your one
namespace you fix **on this run**, through your broker — every mutation brokered, journaled and
reversible (invariant 3). Deferring an in-scope fix to the next heartbeat is a defect (02 §2.5.1).

---

## Execution Checklist

### 1. Confirm scope (your one namespace only)

- You steward **one** namespace. Sweep only the workloads inside that single namespace, using native
  read-only tooling (`kubectl get -n <namespace>`, GKE monitoring). You **cannot** read other namespaces
  or any cluster-scoped resource — do not enumerate or act on anything beyond your namespace. A finding
  that implies a cluster-scoped fix goes one hop up to your parent Cluster Admin Agent with the
  **`escalate`** skill.

### 2. Consult knowledge (optional, read-only)

- Use the **`read-knowledge`** skill to pull any relevant `runbook` for a symptom you find. OKF is the
  knowledge layer — SOPs, blueprints, runbooks — not a mailbox or a coordination channel.

### 3. Run due checks (only what triggers/cron didn't cover)

Read-only, scoped to your one namespace — skip anything an event trigger or a scheduled cron audit
already handles this cycle:

- **Workload health / reliability:** no crash-looping / stuck-pending workloads lingering **past what the
  namespace watch already reacted to** (the watch is the fast path; the heartbeat catches the ones that
  never fired an Event or that the watcher missed).
- **Workload security posture:** no workload has quietly regressed to `privileged`, host-level access
  (`hostNetwork`/`hostPID`/`hostIPC`/`hostPath`), or root (`runAsNonRoot` absent/false) since the last
  scheduled compliance audit.
- **Cost / right-sizing:** workloads whose requests/limits drifted far from actual usage (a lightweight
  backstop to the scheduled obtainability audit).

### 4. Update heartbeat state

- Record this run's outcome and the last-checked timestamps in the agent's heartbeat state
  (`/opt/data/memory/heartbeat-state.json`) so the next sweep knows what was already covered and the
  worst-case latency stays bounded.

### 5. Act on what you found

- **Healthy:** respond **`NO_REPLY`** — a clean backstop sweep is silent, not a notification.
- **Finding within your namespace:** fix it now, with the **`apply-change`** skill
  (`trigger_source: cron`). The Action Broker resolves your scope, classifies the risk, plans the undo,
  gates what needs a human, executes, verifies and journals an `ActionRecord`. You never `apply`/`edit`/
  `patch`/`delete` a live object yourself — the identity in your pod has no write verb — and you never
  open a pull request or an issue for a correction you are allowed to make.
- **Finding that needs a cluster-scoped change:** raise it to your parent Cluster Admin Agent with the
  **`escalate`** skill — a direct, synchronous, one-hop call to your `parentRef`. Act on the structured
  reply (`accepted` / `gated` / `refused` / `timeout` / `paused` / `unreachable`): report a refusal
  verbatim, never retry it in a different shape, never route around a pause, and never block on a
  timeout. As the leaf tier you have nothing to delegate to; the only cross-tier call you make is
  upward.
- Report in four beats (02 §2.5.4) — what you noticed, what you did with its `ActionRecord` ID, how you
  verified it, and the undo handle (`/kage undo <action-id>`). Surface only **concise blockers** for
  anything a human must see; keep the routine noise out.
