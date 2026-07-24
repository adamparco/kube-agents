# SOP: Heartbeat Sweep (Backstop — Cluster Scope)

**Purpose:** A periodic **backstop** sweep of **your one cluster** that catches drift or degradation **no
event trigger and no scheduled cron already covered**, and bounds worst-case detection latency. This is
the **last resort**, not the primary mechanism (04 §4): event triggers (Kubernetes watches, alerts,
GitHub webhooks) are the fast reactive path, cron handles genuinely scheduled audits, and the heartbeat
only sweeps for what slipped past both. A poll lags a fast-moving problem and burns cycles when nothing
changed — so keep it light and rely on it only as the safety net.

You are **read-only** and **cluster-scoped**. Anything this sweep wants to change flows through the
propose→review→reconcile loop as a reviewed PR — never a direct mutation (04 §4/§9; invariant 1).

---

## Execution Checklist

### 1. Confirm scope (your cluster only)

- You steward **one cluster**. Sweep only this cluster's nodes, cluster-scoped objects, and the workloads
  in its namespaces, using native read-only tooling (`kubectl get`, GKE monitoring). Do **not** reach into
  other clusters or fleet-level resources — those belong to a higher tier. A finding that needs a
  fleet-wide change is **escalated upward** with the `raise-escalation` skill, never actioned here.

### 2. Consult knowledge (optional, read-only)

- Use the **`read-knowledge`** skill to pull any relevant `runbook` for a symptom you find, and to check
  whether an open `escalation`/`observation` already tracks it (avoid raising a duplicate). This read path
  can never become a write path (sparse, read-only checkout).

### 3. Run due checks (only what triggers/cron didn't cover)

Read-only, scoped to this cluster — skip anything an event trigger or a scheduled cron audit already
handles this cycle:

- **Cluster capacity / node health:** nodes `Ready`, no sustained pressure/`NotReady`, no un-schedulable
  backlog the autoscaler hasn't resolved.
- **Workload reliability (cluster-wide):** no crash-looping / stuck-pending workloads lingering **past
  what a watch already reacted to** (the watch is the fast path; the heartbeat catches the ones that
  never fired an Event or that no watcher saw).
- **Config-vs-blueprint drift:** cluster-level config still matches its declared blueprint (a lightweight
  backstop to the Platform Agent's authoritative drift-detection sweep; propose, never fix).

### 4. Update heartbeat state

- Record this run's outcome and the last-checked timestamps in the agent's heartbeat state
  (`/opt/data/memory/heartbeat-state.json`) so the next sweep knows what was already covered and the
  worst-case latency stays bounded.

### 5. Respond or propose

- **Healthy:** respond **`NO_REPLY`** — a clean backstop sweep is silent, not a notification.
- **Finding within your cluster authority:** propose the correction as a reviewed PR via the
  **`submit-suggestion`** skill (in your `cluster-admin-agent/` branch namespace). Never `apply`/`edit`/
  `patch`/`delete` a live object — merging the PR reconciles the cluster through the normal GitOps
  rollout.
- **Finding that needs a fleet-wide change:** raise it to the Platform Agent with the
  **`raise-escalation`** skill (an OKF `escalation` entry via PR) — never contact another agent directly
  (invariant 3).
- Surface only **concise blockers** for anything a human must see; keep the routine noise out.
