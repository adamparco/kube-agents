# SOP: Drift Detection (Proactive, Unprompted Correction)

**Purpose:** Detect where the **live** fleet has drifted from its **GitOps-desired** state and open a
**corrective PR unprompted** — while never touching the live object. This is the platform tier's SC4
guarantee (01 §7): the system self-heals through reviewed GitOps, not direct mutation (04 §5.1).

---

## Execution Checklist

### 1. Enumerate the desired objects (read-only)

- For each object the fleet's GitOps repo declares — focus on **RBAC** (ClusterRoles/Bindings),
  **NetworkPolicy**, and **version/config** objects — read its declared desired manifest from the repo.

### 2. Capture live state read-only

- Capture the live object with a **read-only** `get`, e.g.:
  ```bash
  kubectl get <kind> <name> [-n <ns>] -o json > /tmp/live.json
  ```
- 🚨 You are **read-only** (invariant 1). Never `apply`, `edit`, `patch`, or `delete` a live object —
  not even to "quickly fix" the drift.

### 3. Diff desired vs. live with the detect-drift skill

- Run the **`detect-drift`** skill; it uses a desired-authoritative, server-default-tolerant diff and a
  canonical ignore-set so benign defaults don't produce false positives:
  ```bash
  ./skills/detect-drift/scripts/detect_drift.py --desired <manifest> --live /tmp/live.json
  ```
- Exit `0` = no drift (stop); exit `2` = drift found (continue).

### 4. Open a corrective PR — never a direct fix

- On drift, produce the corrective PR via the same skill (`--emit-corrective --work-dir <gitops>
--object-path <repo-relative-manifest>`). It re-asserts the desired manifest and records the drift as
  a `knowledge/observation/drift-<slug>.md` entry, then submits through **`submit-suggestion`** (in the
  `platform-agent/` branch namespace).
- The drifted **live object stays exactly as found** — reconciliation happens only when a human merges
  the PR and the normal GitOps rollout re-applies desired. Detect-and-propose, never detect-and-fix.

### 5. Report

- Summarize which objects drifted, the corrective PRs opened (with links / artifact paths), and any
  drift you deliberately did not correct (with the reason) so the trail is auditable.
