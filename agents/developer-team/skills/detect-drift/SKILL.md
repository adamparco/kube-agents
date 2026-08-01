---
name: detect-drift
description: Watch your namespace for drift — stuck rollouts, single-replica Deployments, containers with no readiness probe, requests and limits far from real usage, images pinned to latest, PVCs nobody consumes, alerts that fire constantly and resolve themselves — and fix what you find through your broker with apply-change. Detection is read-only; remediation is an Action Envelope, never a pull request.
---

# detect-drift — see your workloads drift, then close it

Drift is your namespace diverging from the state you assert over it. You find it with a read-only
`get` and you **fix it**, in the same turn, through `apply-change`. There is no propose step here:
opening a pull request, a GitHub issue, or an OKF entry for work inside your own authority is a
defect on the same footing as a failed action (02 §2.5.1).

You can act this decisively because you are not the thing keeping the namespace safe. Your **broker**
is: it derives your scope from your authenticated identity, classifies the change from the objects
and the diff, checks the brake, generates the undo plan before it executes, and journals the result.
You hold no write credential and you do not set your own risk class. So submit the remediation and
read the answer — do not pre-filter it for yourself.

## What you watch, at workload scope

Your reader identity stops hard at the namespace edge (03 §4.2). One namespace, and **no
cluster-scoped object at all** — nodes, node pools, StorageClasses, cluster add-ons and every other
namespace are refused by RBAC, not merely discouraged. Everything you watch is inside your walls.

| Subject                | The drift                                              | What you do about it                                                            |
| ---------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------- |
| **Stuck rollouts**     | A rollout has been part-way past its progress deadline | Roll back to the last healthy revision — **unless it is capacity**, below       |
| **Single replica**     | A Deployment runs one replica with no declared reason  | `scale` it to at least `--min-replicas`                                         |
| **No readiness probe** | A container takes traffic before it can serve it       | `patch` in an httpGet probe on the health path it already exposes               |
| **Right-sizing**       | Requests or limits are far from observed P95 usage     | `patch` requests toward P95 plus headroom, raising a memory limit that is under |
| **Unpinned images**    | An image is on `latest`, or carries no tag at all      | `patch` it to the digest already running                                        |
| **Orphaned PVCs**      | A PVC has had no consumer for a sustained window       | Submit the delete — it is **gated**, and that is the point                      |
| **Untuned alerts**     | An alert fires constantly and resolves itself          | `patch` its `for` past how long its own firings last                            |

**A stuck rollout whose new pods cannot be scheduled is not yours to fix.** That is node capacity,
and 02 §5 says escalate to the Cluster Admin Agent rather than attempt it — rolling back would hide a
cluster problem behind a workload fix. Use [escalate](../escalate/SKILL.md), one hop, and report what
the callee answered without reaching past your namespace to check.

**Deleting a PVC is gated for you, and you submit it anyway.** A PVC is stateful and not
reconstructable, so your broker parks it for a human (02 §5). Submit it, say plainly that nothing has
been deleted yet, and hand over the approve handle. A gated remediation you quietly skipped is a
decision the team never got to make — and shrinking a change so it slips under a threshold is worse
than skipping it.

## False-positive-resistant by construction

- **Desired-authoritative diff.** Drift is "does every field the manifest specifies still match
  live?" Fields live adds that desired never specified — server defaults like
  `terminationGracePeriodSeconds`, controller-added fields — are **not** drift, so a benign default
  never produces a remediation that changes nothing.
- **Canonical ignore-set.** `status`, `managedFields`, `resourceVersion`, `uid`,
  `creationTimestamp`, `generation`, `selfLink`, and the noisy `last-applied-configuration` /
  `revision` annotations are stripped from both sides before diffing.
- **Nothing is guessed.** A readiness probe with the wrong path is an outage dressed as a fix, and a
  guessed image tag is a deploy nobody asked for. Where the script would have to invent the fix, it
  reports the finding **blocked on the one fact it is missing** instead.
- **Declared singletons are not findings.** A workload that genuinely cannot run two replicas is
  marked `"singleton": true` in the inventory and is left alone.
- **Units are given, not parsed.** CPU is millicores and memory is MiB, as plain integers, on both
  the request side and the observed side.

## Execution Instructions

### 1. Capture the state, read-only

```bash
# One declared manifest against live:
kubectl get deployment api -n team-x -o json > /tmp/live.json

# Or a namespace inventory you assemble from read-only gets:
cat > /tmp/namespace.json <<'JSON'
{
  "namespace": "team-x",
  "workloads": [
    {
      "kind": "Deployment",
      "name": "api",
      "replicas": 1,
      "rollout": { "inProgress": false },
      "containers": [
        {
          "name": "api",
          "image": "us-east4-docker.pkg.dev/acme/app/api:latest",
          "runningDigest": "sha256:6f2c00",
          "requests": { "cpu": 1000, "memory": 2048 },
          "limits": { "memory": 2048 },
          "usageP95": { "cpu": 120, "memory": 640 },
          "healthPath": "/healthz",
          "healthPort": 8080
        }
      ]
    }
  ],
  "persistentVolumeClaims": [
    { "name": "old-cache", "consumers": [], "unusedDays": 41, "capacity": "50Gi", "uid": "3f9a-0001" }
  ],
  "alerts": [
    {
      "name": "ApiLatencyHigh",
      "firesPerDay": 22,
      "actionedRatio": 0.0,
      "forSeconds": 60,
      "p90SelfResolveSeconds": 240,
      "rulePath": "/spec/groups/0/rules/2",
      "target": {
        "group": "monitoring.coreos.com",
        "version": "v1",
        "kind": "PrometheusRule",
        "namespace": "team-x",
        "name": "api-alerts"
      }
    }
  ]
}
JSON
```

### 2. Detect (exit 0 = clean, 2 = drift, 1 = error)

```bash
./skills/detect-drift/scripts/detect_drift.py \
  --desired ./manifests/api-deployment.yaml --live /tmp/live.json

./skills/detect-drift/scripts/detect_drift.py --namespace-state /tmp/namespace.json
```

Flags: `--desired` + `--live` (one object against its manifest), `--namespace-state` (the inventory
survey), `--min-replicas` (default 2), `--overprovision-factor` / `--underprovision-factor` (the
right-sizing band, default 2.5x and 1.1x of P95), `--orphan-days` (default 14), `--stuck-minutes`
(default 30), `--noisy-fires-per-day` / `--actioned-ratio` (default 6/day at under 10% actioned),
`--json` (the whole report), `--emit-operations` (only the operations, ready to submit).

### 3. Remediate through the broker

```bash
./skills/detect-drift/scripts/detect_drift.py --namespace-state /tmp/namespace.json --emit-operations
```

Pass those operations straight to [apply-change](../apply-change/SKILL.md):

```
submit_action(
  intent="Right-size the api container in team-x — it requests 1 CPU and 2Gi and uses a tenth of it",
  trigger_source="cron",
  trigger_ref="drift-detection",
  operations=[ ...the emitted operations... ],
)
```

`trigger_source` is `cron` on the sweep and `watch` when an object change put you in motion — never
`chat`, because nobody asked. Use `plan_action` first when you want the classification and the blast
radius before you promise a human anything. Submit one intent per problem: a right-sizing patch and a
PVC delete are two different conversations, and the second one is going to be parked.

A finding can also come back **blocked on one named fact** — a container's readiness signal, the
digest an unpinned image is running, the last healthy pod template. Go and read that fact, then
remediate. Do not guess it into an envelope.

## After Remediating

Report in the four beats of 02 §2.5.4, one report per intent:

```
What I noticed  — api requested 1000m CPU and 2Gi while its P95 sat at 120m and 640Mi.
What I did      — Cut the requests to 150m and 800Mi (act-4c8e11).
How I verified  — Rollout completed, 2/2 pods Ready, no restarts and no throttling in the 20m since;
                  P95 latency unchanged at 84ms.
Undo            — /kage undo act-4c8e11
```

- **Gated:** the broker parked it. Say what was queued, why it is gated, who was asked, what you did
  in the meantime, and the approve handle. Describe a parked action in the present tense, never the
  past — nothing has changed yet.
- **Refused:** report the reason verbatim and escalate or name the human path. Re-submitting a
  refused remediation in a different shape is a security event, not persistence (02 §2.2).
- **Escalated:** attribute the outcome to the Cluster Admin Agent with **its** `ActionRecord` handle,
  and do not re-verify by reaching past your namespace (02 §2.5.5 rule 5).
- **Still drifting:** if verification did not pass, say so first and plainly. Never call a
  remediation a fix before the broker's verification says it is one.
