# SOP: Drift Detection (Proactive, Unprompted Correction)

**Purpose:** Detect where the **live** fleet has drifted from the state you assert over it and
**close the drift in the same turn**, through your broker. This is the platform tier's SC4 guarantee
(01 §7): the system self-heals by acting, and every mutation is brokered, journaled and reversible
(invariant 3). Detection is read-only; remediation is an Action Envelope, never a pull request.

---

## Execution Checklist

### 1. Enumerate the desired objects (read-only)

- For each object the fleet asserts — focus on **RBAC** (ClusterRoles/Bindings), **NetworkPolicy**,
  and **version/config** objects — read the desired manifest from the IaC mirror (04 §6).

### 2. Capture live state read-only

- Capture the live object with a **read-only** `get`, e.g.:
  ```bash
  kubectl get <kind> <name> [-n <ns>] -o json > /tmp/live.json
  ```
- 🚨 Never `apply`, `edit`, `patch` or `delete` a live object from this process. The identity you hold
  has no write verb, so the attempt fails and is logged. Fixing the drift is step 4, and it goes
  through the broker.

### 3. Diff desired vs. live with the detect-drift skill

- Run the **`detect-drift`** skill; it uses a desired-authoritative, server-default-tolerant diff and a
  canonical ignore-set so benign defaults don't produce false positives:
  ```bash
  ./skills/detect-drift/scripts/detect_drift.py --desired <manifest> --live /tmp/live.json
  ```
- Exit `0` = no drift (stop); exit `2` = drift found (continue).

### 4. Fix it — one envelope per drifted object

- Emit the operations (`--emit-operations`) and submit them with the **`apply-change`** skill,
  `trigger_source: cron` on this sweep (`watch` when an object change put you in motion — never
  `chat`, because nobody asked). Use `plan_action` first when you want the classification and the
  blast radius before you promise anyone anything.
- The broker resolves your scope, classifies the change, checks the brake, **generates the undo plan
  before it executes**, verifies, and journals an `ActionRecord`. Re-asserting a tightened control is
  routine; loosening one is gated. You do not decide which, and you never withhold a remediation
  because you expect it to be gated — submit it and report what came back.
- **Drift that is not yours to apply:** namespace-scoped tenancy objects and cluster internals are
  outside your templated write surface, and your broker refuses them. **Delegate** those to the owning
  Cluster Admin Agent in one hop and report what the callee answered. A cluster with no Cluster Admin
  Agent is `provision-cluster-admin`'s subject, not a bundle you hand-build.
- Never open a pull request, a GitHub issue or an OKF entry for drift you are allowed to fix — that is
  a defect on the same footing as a failed action (02 §2.5.1).

### 5. Report

Four beats (02 §2.5.4), one report per intent: what you noticed, what you did with its `ActionRecord`
ID, how you verified it, and the undo handle (`/kage undo <action-id>`).

- **Gated:** say what was queued, why it is gated, who was asked, and what you did in the meantime.
  Nothing has changed yet — do not describe it in the past tense.
- **Refused:** report the reason verbatim. Do not re-submit the same intent in a different shape.
- **Delegated:** attribute the outcome to the callee with **its** handle, and do not re-verify by
  reaching into its scope.
- **Still drifting:** say so first and unsoftened. Never call a remediation a fix before verification
  says it is one.
