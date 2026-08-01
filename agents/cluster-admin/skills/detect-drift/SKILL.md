---
name: detect-drift
description: Watch your cluster for drift — namespaces with no ResourceQuota, LimitRange or NetworkPolicy, add-ons behind their supported version, node pools chronically over- or under-provisioned, workloads with no PodDisruptionBudget ahead of an upgrade, a namespace with no Developer Team Agent — and fix what you find through your broker with apply-change. Detection is read-only; remediation is an Action Envelope, never a pull request.
---

# detect-drift — see your cluster drift, then close it

Drift is your cluster diverging from the state you assert over it. You find it with a read-only `get`
and you **fix it**, in the same turn, through `apply-change`. There is no propose step here: opening
a pull request, a GitHub issue, or an OKF entry for work inside your own authority is a defect on the
same footing as a failed action (02 §2.5.1).

You can act this decisively because you are not the thing keeping the cluster safe. Your **broker**
is: it derives your scope from your authenticated identity, classifies the change from the objects
and the diff, checks the brake, generates the undo plan before it executes, and journals the result.
You hold no write credential and you do not set your own risk class. So submit the remediation and
read the answer — do not pre-filter it for yourself.

## What you watch, at cluster scope

Your reader identity spans this one cluster (03 §4.2): every namespace in it, its node pools, its
add-ons, and the `Agent` CRs beneath you. That breadth lets you notice things you may not do — you
provision and bound a namespace, then its Developer Team Agent operates the workloads inside it.

| Subject                       | The drift                                                              | What you do about it                                                         |
| ----------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **Tenancy baseline gaps**     | A namespace runs with no ResourceQuota, LimitRange, or NetworkPolicy   | `create` the missing objects from the published baseline                     |
| **Add-on version lag**        | An add-on is behind the version its channel supports                   | `patch` the workload to the supported image                                  |
| **Node-pool provisioning**    | A pool has sat outside the utilization band for a sustained window     | Resize it toward the target utilization                                      |
| **Missing PDBs**              | A multi-replica tenant workload has no PDB and an upgrade is scheduled | **Delegate to that namespace's Developer Team Agent** — see below            |
| **A namespace with no agent** | A tenant namespace has no Developer Team Agent (02 §6)                 | Hand off to [provision-developer-team](../provision-developer-team/SKILL.md) |

**Two of those five are deliberately not yours to apply, and the distinction is enforced, not
polite.** A PodDisruptionBudget is a namespace-scoped object about somebody else's workload: operate
the cluster, delegate the workload (02 §4). Submitting it yourself would be you reaching into a
tenant, and your broker would refuse it. [Delegate](../delegate/SKILL.md) the gap in one hop
(02 §2.3), report what the callee answered, and do not re-verify by reaching into its namespace.
Likewise, a missing child agent is `provision-developer-team`'s whole subject — it renders the child
bundle from the tier template, which is the mechanism that makes an over-grant inexpressible
(03 §4.2). A second copy of that bundle, hand-built here, would be a second copy that drifts.

**You never hand-author a tenancy baseline in this skill.** The quota, limit range and default-deny
policy are defined by the Platform Agent and rendered by `provision-developer-team`. Pass them in
with `--baseline`; a third copy is a third thing to drift. If you have not got the baseline, the
finding comes back **blocked on it** — go and read it, then remediate.

## False-positive-resistant by construction

- **Desired-authoritative diff.** Drift is "does every field the baseline specifies still match
  live?" Fields live adds that desired never specified — server defaults like
  `terminationGracePeriodSeconds`, controller-added fields — are **not** drift, so a benign default
  never produces a remediation that changes nothing.
- **Canonical ignore-set.** `status`, `managedFields`, `resourceVersion`, `uid`,
  `creationTimestamp`, `generation`, `selfLink`, and the noisy `last-applied-configuration` /
  `revision` annotations are stripped from both sides before diffing.
- **Chronic, not spiky.** A node pool is only reported when its measurement window is at least
  `--sustained-days` long, and never when the resize rounds back to the size it already is. Resizing
  on a spike is how an agent spends its initiative budget oscillating.

## Execution Instructions

### 1. Capture the state, read-only

```bash
# One declared object against live:
kubectl get resourcequota -n team-x tenant-quota -o json > /tmp/live.json

# Or a cluster inventory you assemble from read-only gets:
cat > /tmp/cluster.json <<'JSON'
{
  "cluster": "cluster-a",
  "namespaces": [
    {
      "name": "team-x",
      "tenant": true,
      "resourceQuota": true,
      "limitRange": false,
      "networkPolicies": 0,
      "developerTeamAgent": "developer-team-team-x"
    }
  ],
  "addons": [
    {
      "name": "metrics-server",
      "installedVersion": "0.6.4",
      "supportedVersion": "0.7.2",
      "container": "metrics-server",
      "supportedImage": "registry.k8s.io/metrics-server/metrics-server:v0.7.2",
      "target": {
        "group": "apps",
        "version": "v1",
        "kind": "Deployment",
        "namespace": "kube-system",
        "name": "metrics-server"
      }
    }
  ],
  "nodePools": [
    {
      "name": "batch",
      "nodeCount": 12,
      "utilizationP95": 0.18,
      "windowDays": 14,
      "resource": "projects/acme-prod/locations/us-east4/clusters/cluster-a/nodePools/batch"
    }
  ],
  "upgrade": { "planned": true, "targetVersion": "1.31" },
  "workloads": [
    {
      "namespace": "team-x",
      "kind": "Deployment",
      "name": "api",
      "replicas": 4,
      "podDisruptionBudget": false
    }
  ]
}
JSON
```

### 2. Detect (exit 0 = clean, 2 = drift, 1 = error)

```bash
./skills/detect-drift/scripts/detect_drift.py \
  --desired ./baselines/team-x/quota.yaml --live /tmp/live.json

./skills/detect-drift/scripts/detect_drift.py --cluster /tmp/cluster.json --baseline /tmp/baseline.json
```

Flags: `--desired` + `--live` (one object against its declaration), `--cluster` (the inventory
survey), `--baseline` (the tenancy objects a namespace gets), `--min-utilization` /
`--max-utilization` / `--sustained-days` (the node-pool band, default 35–85% over 7 days), `--json`
(the whole report), `--emit-operations` (only the operations, ready to submit).

### 3. Remediate through the broker

```bash
./skills/detect-drift/scripts/detect_drift.py \
  --cluster /tmp/cluster.json --baseline /tmp/baseline.json --emit-operations
```

Pass those operations straight to [apply-change](../apply-change/SKILL.md):

```
submit_action(
  intent="Give team-x the LimitRange and default-deny NetworkPolicy its tenancy baseline requires",
  trigger_source="cron",
  trigger_ref="drift-detection",
  operations=[ ...the emitted operations... ],
)
```

`trigger_source` is `cron` on the sweep and `watch` when an object change put you in motion — never
`chat`, because nobody asked. Use `plan_action` first when you want the classification and the blast
radius before you promise a human anything.

A finding can also come back **blocked on one named fact** — a node pool's cloud resource path, an
add-on's supported image, the tenancy baseline itself. Go and read that fact, then remediate. Do not
guess it into an envelope.

## After Remediating

Report in the four beats of 02 §2.5.4, one report per intent:

```
What I noticed  — team-x had a ResourceQuota but no LimitRange and no NetworkPolicy.
What I did      — Created both from the published tenancy baseline (act-9a2b40).
How I verified  — Both objects present in team-x; the default-deny policy selects all pods; no
                  workload in the namespace has restarted in the 15m since.
Undo            — /kage undo act-9a2b40
```

- **Gated:** the broker parked it. Say what was queued, why it is gated, who was asked, what you did
  in the meantime, and the approve handle. A gated remediation is submitted and reported — never
  skipped, and never re-shaped into something that would classify lower. A node-pool resize is the
  usual one; do not shrink the resize to slip under a threshold.
- **Refused:** report the reason verbatim and escalate or name the human path. Re-submitting a
  refused remediation in a different shape is a security event, not persistence (02 §2.2).
- **Delegated:** attribute the outcome to the callee with **its** `ActionRecord` handle, and do not
  re-verify by reaching into its namespace (02 §2.5.5 rule 5).
- **Still drifting:** if verification did not pass, say so first and plainly. Never call a
  remediation a fix before the broker's verification says it is one.
