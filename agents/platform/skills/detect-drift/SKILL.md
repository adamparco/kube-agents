---
name: detect-drift
description: Watch the fleet for drift — version skew across clusters, tenancy baselines missing in a namespace you govern, executed state diverging from the IaC mirror, a cluster with no Cluster Admin Agent — and fix what you find through your broker with apply-change. Detection is read-only; remediation is an Action Envelope, never a pull request.
---

# detect-drift — see the fleet drift, then close it

Drift is your scope diverging from the state you assert over it. You find it with a read-only `get`
and you **fix it**, in the same turn, through `apply-change`. There is no propose step here: opening
a pull request, a GitHub issue, or an OKF entry for work inside your own authority is a defect on the
same footing as a failed action (02 §2.5.1).

You can act this decisively because you are not the thing keeping the fleet safe. Your **broker** is:
it derives your scope from your authenticated identity, classifies the change from the objects and
the diff, checks the brake, generates the undo plan before it executes, and journals the result. You
hold no write credential and you do not set your own risk class. So submit the remediation and read
the answer — do not pre-filter it for yourself.

## What you watch, at fleet scope

Your reader identity is project-wide (03 §3.2): the clusters in your project, their cloud resources,
and the `Agent` CRs beneath you. Four subjects live at that altitude.

| Subject                     | The drift                                                           | What you do about it                                                       |
| --------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **Mirror drift**            | An object's executed state no longer matches the IaC mirror (04 §6) | Re-assert the mirror with `apply`, then mirror the executed state back     |
| **Fleet version skew**      | A cluster's control plane is a minor behind the newest in the fleet | Submit the control-plane upgrade for that cluster                          |
| **Tenancy baseline gaps**   | A namespace you govern is missing a kind its baseline requires      | **Delegate to that cluster's Cluster Admin Agent** — see below             |
| **A cluster with no agent** | A cluster in your project has no Cluster Admin Agent (02 §6)        | Hand off to [provision-cluster-admin](../provision-cluster-admin/SKILL.md) |

**Two of those four are deliberately not yours to apply, and the distinction is enforced, not
polite.** Namespace-scoped tenancy objects and cluster internals are outside your templated write
surface, so submitting them would be **refused** by your broker (02 §3, 03 §3.2). You define the
tenancy model; the Cluster Admin Agent applies it. [Delegate](../delegate/SKILL.md) the gap in one
hop (02 §2.3), report what the callee answered, and do not reach into the cluster yourself.
Likewise, a missing child agent is `provision-cluster-admin`'s whole subject — it renders the child
bundle from the tier template, which is the mechanism that makes an over-grant inexpressible
(03 §4.2). A second copy of that bundle, hand-built here, would be a second copy that drifts.

## False-positive-resistant by construction

- **Desired-authoritative diff.** Drift is "does every field the mirror specifies still match live?"
  Fields live adds that desired never specified — server defaults like
  `terminationGracePeriodSeconds`, controller-added fields — are **not** drift, so a benign default
  never produces a remediation that changes nothing.
- **Canonical ignore-set.** `status`, `managedFields`, `resourceVersion`, `uid`,
  `creationTimestamp`, `generation`, `selfLink`, and the noisy `last-applied-configuration` /
  `revision` annotations are stripped from both sides before diffing.
- **Skew needs a fleet.** A single cluster, or one whose version string will not parse, is never
  reported as skewed against itself.

## Execution Instructions

### 1. Capture the state, read-only

```bash
# One object against its mirror:
kubectl get networkpolicy -n team-x default-deny -o json > /tmp/live.json

# Or a fleet inventory you assemble from read-only gets — clusters, child agents, governed namespaces:
cat > /tmp/fleet.json <<'JSON'
{
  "projectId": "acme-prod",
  "clusters": [
    { "name": "cluster-a", "location": "us-east4", "controlPlaneVersion": "1.31.4-gke.1183000" },
    { "name": "cluster-b", "location": "us-west1", "controlPlaneVersion": "1.29.9-gke.1500000" }
  ],
  "agents": [{ "tier": "cluster-admin", "cluster": "cluster-a" }],
  "governedNamespaces": [
    {
      "cluster": "cluster-a",
      "namespace": "tenant-a",
      "baseline": ["NetworkPolicy", "ResourceQuota", "LimitRange"],
      "present": ["ResourceQuota"]
    }
  ]
}
JSON
```

### 2. Detect (exit 0 = clean, 2 = drift, 1 = error)

```bash
./skills/detect-drift/scripts/detect_drift.py \
  --desired ./mirror/clusters/cluster-a/namespaces/team-x/netpol.yaml --live /tmp/live.json

./skills/detect-drift/scripts/detect_drift.py --fleet /tmp/fleet.json
```

Flags: `--desired` + `--live` (one object against the mirror), `--fleet` (the inventory survey),
`--json` (the whole report), `--emit-operations` (only the operations, ready to submit).

### 3. Remediate through the broker

```bash
./skills/detect-drift/scripts/detect_drift.py --fleet /tmp/fleet.json --emit-operations
```

Pass those operations straight to [apply-change](../apply-change/SKILL.md):

```
submit_action(
  intent="Upgrade cluster-b's control plane to 1.31 so the fleet runs one minor",
  trigger_source="cron",
  trigger_ref="drift-detection",
  operations=[ ...the emitted operations... ],
)
```

`trigger_source` is `cron` on the sweep and `watch` when an object change put you in motion — never
`chat`, because nobody asked. Use `plan_action` first when you want the classification and the blast
radius before you promise a human anything.

A finding can also come back **blocked on one named fact** — a cluster's cloud resource path, for
instance. Go and read that fact, then remediate. Do not guess it into an envelope.

## After Remediating

Report in the four beats of 02 §2.5.4, one report per intent:

```
What I noticed  — cluster-b's control plane was on 1.29 while the rest of the fleet ran 1.31.
What I did      — Submitted the upgrade to 1.31 for cluster-b (act-7f3c21).
How I verified  — Control plane reports 1.31.4-gke.1183000; all three node pools Ready for 12m.
Undo            — /kage undo act-7f3c21
```

- **Gated:** the broker parked it. Say what was queued, why it is gated, who was asked, what you did
  in the meantime, and the approve handle. A gated remediation is submitted and reported — never
  skipped, and never re-shaped into something that would classify lower.
- **Refused:** report the reason verbatim and escalate or name the human path. Re-submitting a
  refused remediation in a different shape is a security event, not persistence (02 §2.2).
- **Delegated:** attribute the outcome to the callee with **its** `ActionRecord` handle, and do not
  re-verify by reaching into its scope (02 §2.5.5 rule 5).
- **Still drifting:** if verification did not pass, say so first and plainly. Never call a
  remediation a fix before the broker's verification says it is one.
