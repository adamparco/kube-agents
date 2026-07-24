---
name: detect-drift
description: Detect divergence between GitOps-desired state and the live cluster (read-only) and open a corrective Pull Request — unprompted, and without ever patching the live object. This is how the platform tier reconciles drift while staying strictly read-only.
---

# detect-drift — read-only drift detection → corrective PR

Configuration drift is when the **live** cluster no longer matches the **GitOps-desired** state (someone
edited an object directly, a controller mutated it, a version skewed). This skill finds that drift with
a **read-only** `get` and proposes the fix as a **reviewed PR** — it never touches the live object
(invariant 1; SC4, 01 §7; 04 §5.1).

## When to Use

- On the platform **drift-detection SOP** sweep (cron), for RBAC, NetworkPolicy, and version objects the
  fleet's GitOps repo declares.
- Any time you need to confirm whether a live object still matches its declared desired state before
  acting on it.

## Read-only and false-positive-resistant by construction

- **Never mutates live.** The script only reads the live object (you pass it the output of
  `kubectl get <obj> -o json`). The correction is a PR; the drifted live object is left as found.
- **Desired-authoritative diff.** Drift is "does every field the GitOps manifest specifies still match
  live?" Fields live adds that desired never specified (server defaults like
  `terminationGracePeriodSeconds`, controller-added fields) are **not** drift — so benign defaults don't
  open noisy false-positive PRs.
- **Canonical ignore-set.** `status`, `managedFields`, `resourceVersion`, `uid`, `creationTimestamp`,
  `generation`, `selfLink`, and the noisy `last-applied-configuration` / `revision` annotations are
  stripped before diffing.

## Execution Instructions

```bash
# 1. Capture live state read-only:
kubectl get networkpolicy -n team-x default-deny -o json > /tmp/live.json

# 2. Diff against the GitOps-desired manifest (exit 0 = clean, 2 = drift, 1 = error):
./skills/detect-drift/scripts/detect_drift.py \
  --desired ./gitops/clusters/cluster-a/namespaces/team-x/netpol.yaml \
  --live /tmp/live.json

# 3. On drift, emit a corrective PR (or, hermetically, a dry-run artifact):
./skills/detect-drift/scripts/detect_drift.py \
  --desired ./gitops/.../netpol.yaml --live /tmp/live.json \
  --emit-corrective --work-dir ./gitops \
  --object-path clusters/cluster-a/namespaces/team-x/netpol.yaml \
  --dry-run --artifact-dir ./.drift-artifact
```

Flags: `--desired`, `--live`, `--json`, `--emit-corrective`, `--work-dir` (GitOps working tree),
`--object-path` (repo-relative desired manifest to re-assert), `--slug`, `--created`, `--dry-run`,
`--artifact-dir`.

## After Detecting

The corrective PR re-asserts the desired manifest and records the drift as a
`knowledge/observation/drift-<slug>.md` entry for the audit trail. Never patch the live object directly
— merging the PR reconciles the cluster via the normal GitOps rollout.
