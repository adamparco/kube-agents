# SOP: Lifecycle / Deprecation Manager (Monthly Governance)

**Purpose:** Proactively scans manifests fleet-wide for deprecated Kubernetes API versions and **migrates them** — or gets them migrated by the tier that owns them — before the impending GKE cluster upgrade removes the API.

---

## Execution Checklist

### 1. Identify Target GKE Version Upgrades

- Scan GKE server configurations to identify the next target GKE upgrade version (e.g. upgrading from `1.28` to `1.29`).
- Identify **Impending API Deprecations** in the target version (e.g., `flowcontrol.apiserver.k8s.io/v1beta2` is deprecated in `1.29`).

### 2. Scan Application Workload Manifests

For each active namespace in the fleet:

1.  Inspect workload manifests directly using native GKE monitoring and read-only tools:
2.  Inspect all resource API versions (`apiVersion` keys).
3.  Identify any resources using the deprecated API versions.

### 3. Migrate Them

A deprecation you warned about and left in place will still break on upgrade day. Notifying a team is something you do **in addition to** the fix, never instead of it (02 §2.5.1).

1.  **Migrate what is yours:** for every object in your project scope still on a removed API version, submit the migration to the current `apiVersion` with your **`apply-change` skill** (`trigger_source: cron`). An in-place API version migration of an object that already exists is typically routine; the broker classifies it, plans the undo and journals an `ActionRecord`.
2.  **Delegate the rest, one hop:** workload manifests inside a cluster's namespaces are not yours to apply — your broker refuses them. **Delegate** each cluster's list to its Cluster Admin Agent, which can reach the namespaces or pass it further down, and report what the callee answered. A delegated migration is still your responsibility to track until upgrade day.
3.  **Tell the owning teams too** — with the migration already submitted or delegated, not as a request for them to do it.

### 4. Report

Four beats (02 §2.5.4): which APIs the next version removes and what still uses them, what you migrated with each `ActionRecord` ID, how you verified it, and the undo handle (`/kage undo <action-id>`). Then the part that matters most — **what remains at risk before the upgrade date**, who owns it, and what the broker gated or refused. Unfinished work first, unsoftened.
