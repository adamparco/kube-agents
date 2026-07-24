# SOP: Heartbeat Sweep (Backstop — Namespace Scope)

**Purpose:** A periodic **backstop** sweep of **your one assigned namespace** that catches drift or
degradation **no event trigger and no scheduled cron already covered**, and bounds worst-case detection
latency. This is the **last resort**, not the primary mechanism (04 §4): event triggers (a namespace-
scoped Kubernetes watch, alerts, GitHub webhooks) are the fast reactive path, cron handles genuinely
scheduled audits, and the heartbeat only sweeps for what slipped past both. A poll lags a fast-moving
problem and burns cycles when nothing changed — so keep it light and rely on it only as the safety net.

You are **read-only** and **namespace-scoped**. Anything this sweep wants to change flows through the
propose→review→reconcile loop as a reviewed PR — never a direct mutation (04 §4/§9; invariant 1).

---

## Execution Checklist

### 1. Confirm scope (your one namespace only)

- You steward **one** namespace. Sweep only the workloads inside that single namespace, using native
  read-only tooling (`kubectl get -n <namespace>`, GKE monitoring). You **cannot** read other namespaces
  or any cluster-scoped resource — do not enumerate or act on anything beyond your namespace. A finding
  that implies a cluster-scoped fix is **escalated** to your parent Cluster Admin Agent with the
  `raise-escalation` skill, never actioned here.

### 2. Consult knowledge (optional, read-only)

- Use the **`read-knowledge`** skill to pull any relevant `runbook` for a symptom you find, and to check
  whether an open `escalation`/`observation` already tracks it (avoid raising a duplicate). This read path
  can never become a write path (sparse, read-only checkout).

### 3. Run due checks (only what triggers/cron didn't cover)

Read-only, scoped to your one namespace — skip anything an event trigger or a scheduled cron audit
already handles this cycle:

- **Workload health / reliability:** no crash-looping / stuck-pending workloads lingering **past what the
  namespace watch already reacted to** (the watch is the fast path; the heartbeat catches the ones that
  never fired an Event or that the watcher missed).
- **Workload security posture:** no workload has quietly regressed to `privileged`, host-level access
  (`hostNetwork`/`hostPID`/`hostIPC`/`hostPath`), or root (`runAsNonRoot` absent/false) since the last
  scheduled compliance audit.
- **Cost / right-sizing:** flag workloads whose requests/limits drifted far from actual usage (a
  lightweight backstop to the scheduled obtainability audit; propose, never fix).

### 4. Update heartbeat state

- Record this run's outcome and the last-checked timestamps in the agent's heartbeat state
  (`/opt/data/memory/heartbeat-state.json`) so the next sweep knows what was already covered and the
  worst-case latency stays bounded.

### 5. Respond or propose

- **Healthy:** respond **`NO_REPLY`** — a clean backstop sweep is silent, not a notification.
- **Finding within your namespace:** propose the correction as a reviewed PR via the
  **`submit-suggestion`** skill (in your `developer-team-agent/` branch namespace). Never `apply`/`edit`/
  `patch`/`delete` a live object — merging the PR reconciles the namespace through the normal GitOps
  rollout.
- **Finding that needs a cluster-scoped change:** raise it to your parent Cluster Admin Agent with the
  **`raise-escalation`** skill (an OKF `escalation` entry via PR) — never contact another agent directly
  (invariant 3).
- Surface only **concise blockers** for anything a human must see; keep the routine noise out.
