# Phase 8 — live-target checklist (L3)

Everything Phase 8 claims that **the inner loop cannot prove**, with the exact command and the exact
observation that would discharge it. `verify-phase8.sh` prints a pointer to this file and asserts
nothing in it.

That list got shorter on 2026-07-26 and the reason is worth stating, because it changes what several
of these steps are for. The inner loop moved from Kind to a remote GKE cluster with **Dataplane V2**
and a **workload pool** (`dev/cluster/up.sh`). Two of the six steps below existed because Kind had no
metadata server and a different NetworkPolicy implementation than the live cluster. Neither is true
of the new inner loop, so those steps stop being _"the substrate cannot do this"_ and become _"this
particular cluster, with its real service accounts and its real policy set, has not been asked"_ —
still a live-only claim, and a much narrower one. Each step's **Why** paragraph says which it is.

This is not a deferrals table. The [deferrals table](LEDGER.md#deferrals) records claims that are
**blocked** on something external and names the blocker. This file records claims that are blocked on
nothing but a human being at a terminal with a real cluster — the work is runnable today, it just
cannot run unattended, so it is written down at the level of detail that lets someone else run it.

## Ground rules, before any of it

`platform-agent-host` (project `adamparco-kage`, us-east4) is **outer-loop install verification
only**. It is not ephemeral and it is **not a destructive-test target** (`binding.md` §Targets).
Nothing in this file scales, deletes, drains, cordons or force-recreates anything on it. Steps that
need a destructive action say so and require a **scratch** cluster (`gke-scratch-*`).

`kubectl config current-context` may well be the live cluster. Every command below passes an explicit
`--context`, and so must anything you type between them.

Two of these steps mint or read real credentials. The secrets live only in the gitignored
`k8s-operator/scripts/vars.sh` and the `platform-agent-secrets` Kubernetes Secret. Do not echo them,
do not paste them into a log you intend to attach, and do not commit `vars.sh`.

---

## L3-1 — Workload Identity still mints a token with the egress policy applied

**Why the inner loop cannot do it.** This is the second half of Accept (d), and it is a conjunction
on purpose: _off-allowlist egress blocked **while Workload Identity still works**_. Either half alone
is easy and wrong. The inner loop proves the blocking half. For the minting half the blocker
**changed and shrank** on 2026-07-26: it used to be that Kind had no metadata server at all, and the
inner-loop cluster is now GKE with a workload pool, so it has one. What is missing is the binding — a
GSA, an IAM policy on it, and the annotation tying the agent KSA to it. That is provisioning nobody
has done on the dev cluster, not a property of the substrate, and doing it would discharge this step
without touching the live install.

This is also the step most likely to fail quietly. A broken metadata allow surfaces as a **timeout
inside the auth client library** — an authentication error that never mentions the network — which is
exactly how the wrong remediation note shipped in the first place (see [phase-8.md](phase-8.md), the
P8-T2 finding).

```sh
CTX=gke_adamparco-kage_us-east4_platform-agent-host

# 1. What dataplane is actually running? The metadata pairing depends on the answer, and this is a
#    fact about the cluster, not about its name.
kubectl --context "$CTX" -n kube-system get pods -o name | grep -iE 'calico|cilium|anetd|netd'

# 2. Apply the tier's egress policy, rendered for THIS cluster.
#    Expect: netpol/<tier>-agent-egress configured — and no REPLACE_WITH_* anywhere in the output.
kubectl --context "$CTX" -n kubeagents-system get netpol -o yaml | grep -c REPLACE_WITH_ # expect 0

# 3. From inside the agent pod, mint a token.
POD=$(kubectl --context "$CTX" -n kubeagents-system get pod \
        -l app=platform-agent -o jsonpath='{.items[0].metadata.name}')
kubectl --context "$CTX" -n kubeagents-system exec "$POD" -c agent -- \
  curl -s -m 10 -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
  | head -c 40
```

**Expected.** Step 3 returns a JSON object beginning `{"access_token":"ya29.` within the 10-second
timeout. **`-m 10` is load-bearing**: without it a blocked metadata route hangs until the client's own
timeout and the failure reads as an auth problem rather than a network one.

**What a failure means.** A timeout here with the policy applied and a success without it means the
metadata allow is wrong for this dataplane — check the pairing (988/987 belong to
`169.254.169.252/32` on Dataplane V1 / Calico; `169.254.169.254/32` takes 80/8080 on Dataplane V2),
not the service account.

**Records.** V-CTN-020 at L3 (currently a carried deferral: this cluster has no Dataplane V2).

---

## L3-2 — Off-allowlist egress is actually blocked on the live dataplane

**Why the inner loop cannot do it.** It largely can, now: the dev cluster runs Dataplane V2, the
same implementation as the live one, so `egress-enforcement-l2.sh` is no longer standing in for a
different dataplane. What is left here is not the mechanism but this cluster's own policy set —
`platform-agent-host` carries the rendered production allow-list, and "the policy is accepted" is
still not "the packet is dropped".

```sh
CTX=gke_adamparco-kage_us-east4_platform-agent-host
POD=$(kubectl --context "$CTX" -n kubeagents-system get pod \
        -l app=platform-agent -o jsonpath='{.items[0].metadata.name}')

# Baseline FIRST: prove the target is reachable with no policy in the way, or a "denied" result
# below means nothing (a negative with no positive control is not evidence).
kubectl --context "$CTX" -n kubeagents-system exec "$POD" -c agent -- curl -s -m 5 -o /dev/null -w '%{http_code}\n' https://1.1.1.1
# ... apply the policy, then:
kubectl --context "$CTX" -n kubeagents-system exec "$POD" -c agent -- curl -s -m 5 -o /dev/null -w '%{http_code}\n' https://1.1.1.1
```

**Expected.** Reachable before, timeout after. Then the on-allowlist host still reachable, and the
allowlisted namespace on a **non**-allowlisted port refused — port narrowing is what makes a narrow
metadata allow meaningfully narrower than a whole-host one.

**Records.** V-CTN-020 at L3.

---

## L3-3 — A clean-clone install brings all three tiers Ready

**Why the inner loop cannot do it.** Accept (a) says _clean clone → all three tiers Ready, from
published images_. The dev cluster runs the operator at a `dev-<sha>` digest built from the working
tree; that is the correct inner loop and it is the opposite of this claim. This step is the only thing that exercises the
published images, the real registry pull path, and the provisioning scripts in the order a new user
meets them.

```sh
git clone https://github.com/adamparco/kube-agents /tmp/kage-clean && cd /tmp/kage-clean
cp ~/secure/vars.sh k8s-operator/scripts/vars.sh   # never from the repo, never committed
KUBE_CONTEXT=gke_adamparco-kage_us-east4_platform-agent-host bash k8s-operator/scripts/provision.sh
```

**Expected.** Every step exits 0, and all three tiers reach Ready:

```sh
kubectl --context "$CTX" get pods -A -l 'kube-agents/tier' \
  -o custom-columns=NS:.metadata.namespace,POD:.metadata.name,TIER:.metadata.labels.kube-agents/tier,READY:.status.containerStatuses[*].ready
```

**Check the image digests are the published ones, not something local:**

```sh
kubectl --context "$CTX" get pods -A -l 'kube-agents/tier' -o jsonpath='{range .items[*]}{.status.containerStatuses[*].imageID}{"\n"}{end}' | sort -u
```

Every line should be a registry digest. A `docker.io/library/...` or a bare tag with no digest means
something was side-loaded and the claim is not proven.

**Records.** V-CMP-001, V-CMP-004, V-CMP-005 at L3.

---

## L3-4 — Every tier completes an inference call and mints a token from its own namespace

**Why the inner loop cannot do it.** Accept (b). Needs a real model endpoint and real per-namespace
Workload Identity bindings. The L2 runs prove the wiring (C5/C6 Wired probes); this proves the call.

Per tier — platform, cluster-admin, developer-team — from a pod **in that tier's own namespace**:

```sh
kubectl --context "$CTX" -n "$NS" exec "$POD" -c agent -- \
  python3 -c "import os,urllib.request,json; ..."   # or the tier's own health/echo path
```

**Expected.** A completion returns, and the token minted in step 1 is scoped to **that namespace's**
service account — not the platform tier's. The cross-tier version of this is the interesting one: a
developer-team pod must **not** be able to mint the cluster-admin tier's token.

**Records.** V-CMP-001 (C5, C6 Exercised probes) at L3.

---

## L3-5 — The published image tags are immutable and match `tags.env`

**Why the inner loop cannot do it.** P8-T5 made `:v0.1.0` real via an immutable scheme (`main` → `:${sha}`;
`v*` git tag → the release tag, with the tag build failing if the git tag disagrees with
`KAGE_IMAGE_VERSION`). The build-side half is gated at L0. The registry-side half — that the tag
exists, resolves to one digest, and has not moved — can only be asked of the registry.

```sh
for img in kage-operator kage-router platform-agent cluster-admin-agent developer-team-agent; do
  echo "== $img"
  gcloud artifacts docker images describe \
    "us-east4-docker.pkg.dev/adamparco-kage/kube-agents/$img:v0.1.0" --format='value(image_summary.digest)'
done
```

**Expected.** Each resolves to exactly one digest, and re-running after a `main` build returns the
**same** digest for `:v0.1.0` (only `:${sha}` should move).

**Records.** V-CMP-002 at L3.

---

## L3-6 — Destructive resilience, on a scratch cluster only

**Not on `platform-agent-host`.** The chaos suite scales and deletes. It is guarded by an anchored
`case` on `gke-scratch-*` and will refuse the live context — which is the guard working, not an
obstacle to route around.

Since 2026-07-26 this needs no special cluster: the inner loop **is** a `gke-scratch-*` Dataplane V2
cluster, so the step is the ordinary L2 run.

```sh
bash dev/cluster/up.sh                                          # idempotent; resume.sh if paused
bash dev/verify/verify-phase8.sh gke-scratch-kube-agents-dev
```

The same cluster is what would discharge **L3-1** and **L3-2** without touching the live install,
which is the cheaper path to closing that deferral — L3-1 needs the GSA/KSA binding described above,
L3-2 needs the production allow-list rendered against it.

**Records.** the scratch-GKE V-G cloud checks; V-CTN-020 at L3.

---

## Status

| Step | Claim                                | Records            | Run? |
| ---- | ------------------------------------ | ------------------ | ---- |
| L3-1 | WI mints a token with egress applied | V-CTN-020 (L3)     | ☐    |
| L3-2 | Off-allowlist egress blocked, live   | V-CTN-020 (L3)     | ☐    |
| L3-3 | Clean-clone install, three tiers     | V-CMP-001/004/005  | ☐    |
| L3-4 | Inference + per-namespace token      | V-CMP-001 (C5/C6)  | ☐    |
| L3-5 | Published tags immutable             | V-CMP-002 (L3)     | ☐    |
| L3-6 | Destructive resilience               | V-G scratch checks | ☐    |

Nothing is ticked. Ticking a box here is a claim, and a claim needs the output pasted into a ledger
row with the date and the cluster — a tick on its own is the shape of evidence without the substance.
