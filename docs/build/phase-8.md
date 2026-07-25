# Phase 8 — Contain the pod, close the boundary, make the install real (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §2 "Phase 8 — Contain the pod, close the
boundary, make the install real". **Goal:** everything that must be true **before** any agent gets
write authority. This phase adds **no imperative capability at all**; it removes the reasons it
would be unsafe to start.

**Why this is the first phase of the imperative conversion.** The live install today has an open
human→agent boundary, no enforced egress, and a multi-tier install that does not work without a
local build. Granting write authority on top of that would compound three known defects instead of
fixing them. The hard ordering constraint in 07 §5 — and `invariants.md` check 7, _authority never
precedes machinery_ — is what makes this ordering non-negotiable rather than a preference.

**Phase ratchet (09 §10):** newly required at the end of this phase — **V-CTN (read-side)**, **V-CTR
core**, **V-CMP**, **V-MET**. No write authority exists yet, so **V-CTN-004** (reader SA holds no
write verb, universally) must be **universally green**, not green per-tier.

---

## Survey of the current state — what exists, what is broken, what is missing

This drives the breakdown. Every claim below is a file:line the survey actually read.

### The `allowedUsers` bypass is open on four layers (P8-T1)

The provisioning template renders a **one-element list containing the empty string** from an unset
`envsubst` variable:

- `k8s-operator/scripts/platform-agent.yaml.template:43-44` → `allowedUsers: [ "${ALLOWED_USERS}" ]`,
  and `:54-55` for Slack. `cluster-admin-agent.yaml.template` and
  `developer-team-agent.yaml.template` carry the same shape.
- `k8s-operator/scripts/provision_05_gcp_gchat.sh:33` and `provision_06_slack.sh:26` **default the
  variable to `""`**, and `provision_08_deploy_platform_agent.sh:89,102` re-export it empty.
- The list is therefore **size 1**, so both guards pass:
  - the webhook, `k8s-operator/internal/webhook/agent_webhook.go:236-255`, tests
    `len(gc.AllowedUsers) == 0`;
  - the CEL rules, `k8s-operator/api/v1alpha1/agent_types.go:41,80`, test
    `size(self.allowedUsers) > 0`.
- The controller then reads the lone blank entry and emits the permissive backstop:
  `k8s-operator/internal/controller/agent_manifests.go:446` (`GOOGLE_CHAT_ALLOW_ALL_USERS`) and
  `:465` (`SLACK_ALLOW_ALL_USERS`).

**This is a real hole on a running deployment**, and in Phase 10 it becomes a write-authority hole.
06 §1.2 **V-7** already states the correct rule — "an all-blank/whitespace list is **not** an
allowlist — it is empty" — so the spec is right and the implementation does not match it. That makes
this a conformance defect, not a design question.

The in-pod authorizer is **not** part of the hole and must stay as it is:
`k8s-operator/internal/router/authorize.go:28-30` matches against the CR's `AllowedUsers` and
deliberately never consults any `*_ALLOW_ALL_USERS` env. The escape hatch is only reachable through
the rendered pod env, which is why deleting it outright is safe.

### Egress policies are inert exemplars (P8-T2)

Three per-tier egress policies exist and are structurally correct —
`examples/gitops-repo/fleet/netpol-platform-egress.yaml`,
`clusters/cluster-a/agents/netpol-cluster-admin-egress.yaml`,
`clusters/cluster-a/namespaces/team-x/30-netpol-developer-team-egress.yaml` — and **all three are
full of `REPLACE_WITH_*` placeholders**. No install path applies any of them, and the live cluster
has no Dataplane V2, so nothing is enforced. This is LSN-006 exactly: well-formed is not enforced.

**The metadata-server conflict must be resolved first.** The policies deny `169.254.169.254`, which
is precisely how Workload Identity mints tokens on GKE. Applying them as written breaks every
tier's identity. Accept (d) is deliberately worded as a conjunction — off-allowlist egress blocked
**while Workload Identity still works** — because either half alone is easy and wrong.

### Tenant isolation manifests are written but never applied (P8-T3)

`examples/gitops-repo/clusters/cluster-a/namespaces/team-x/10-resourcequota.yaml` and
`20-netpol-default-deny.yaml` exist; provisioning skips both.

### A multi-tier install does not work (P8-T4)

- The `mcp-remote` OAuth bridge (`deploy/docker/Dockerfile:80-82`, wired at
  `deploy/shared/defaults/config.yaml:17`) **hangs headless** — it wants an interactive OAuth flow.
- The dashboard container lacks the rendered config and telemetry env the other containers get.
- The `ExternalName` aliases a developer-team agent needs for in-namespace `litellm` /
  `github-token-minter` DNS are asserted by `local-dev/kind/verify-phase3.sh:151-154` but not
  shipped by any install path.
- **LSN-015 (open) binds here.** The first real multi-tier install deadlocked because the
  `system-metadata` PVC is a fixed namespace-scoped name with `ReadWriteOnce` while the data PVC is
  per-agent — so the **second** agent in a namespace hangs in `ContainerCreating`. Every prior
  fixture ran exactly one agent per namespace and could not see it.

### Three images are never published (P8-T5)

`.github/workflows/docker-publish-ghcr.yml` publishes three images; **`kage-router`,
`cluster-admin-agent` and `developer-team-agent` are not among them.** `Makefile:21,32` pushes
`:latest`. This is LSN-007 (built, tested, and unreachable) and the check that owns it is
**V-CMP-002** — the check the router failed for two phases.

### The invariants gate is a checklist, not a mechanism (P8-T6)

`.claude/harness/invariants.md` has already been re-derived from the six new invariants and carries
the two conversion-ordering checks. The remaining work is to make checks **7** (authority never
precedes machinery) and **8** (tests are replaced, never deleted) **mechanical**: a pre-merge script
that fails any diff adding a write verb to an agent identity while the broker, classifier, journal
or undo path is absent, and that flags a net reduction in security assertions. LSN-009 is closed
"check defined; the mechanical pre-merge script lands in P8-T6" — this is that script.

### Documentation describes a retired API (P8-T7)

`INSTALL.md:38,73,276,353,669` still names the retired `PlatformAgent` kind, including a command at
`:276` that fails outright. The pipeline list stops at step 11 though provisioning now has step 12
(`provision_12_deploy_agent_tiers.sh`). The live-install learnings live only in
`k8s-operator/scripts/README.md`.

---

## Planning defect found and resolved: Accept (c) was half-uncovered

`harness-run` §3 requires every acceptance bullet to bind to at least one check ID, and treats an
unbound bullet as a planning defect to resolve **now**.

Accept (c) has two halves. The admission half — _an `Agent` CR with an empty or blank allowlist and
chat enabled is rejected at admission_ — is owned by **V-CTR-002** ("each of V-1…V-10 has a negative
test rejected with the field path in the message"), because 06 §1.2 **V-7** is the closed-allowlist
rule. That half is covered.

The second half — _and no rendered pod carries a `*_ALLOW_ALL_USERS` env_ — **was owned by no check
in 09 §6.** The nearest neighbours do not cover it: V-CTN-033 is about principal _format_ and is
Phase 10; V-CMP-011 is about prohibited field names in the **CRD schema**, not rendered pod env;
V-CMP-003 is about `REPLACE_WITH_*` tokens.

**Resolution: add `V-CTR-014` to 09 §6.9.** The next free ID in the V-CTR suite (max was 13).

> **V-CTR-014** — **The permissive escape hatch does not exist.** No `*_ALLOW_ALL_USERS` identifier
> appears anywhere in the tree — not in the controller's renderer, a template, a provisioning
> script, a config default, or a rendered pod spec — and no pod rendered from any shipped `Agent` CR
> carries such an env var. Negative control: reintroduce the env in a fixture and confirm the check
> fails. `L0, L2` · Phase 8 · gate class BLOCKING-PHASE.

This is a **spec tightening**, legitimate under PROTOCOL §10.5 ("tightening something
unfalsifiable"), and it is made in the PLAN unit — **before** the implementation whose behaviour it
constrains — so §10.1 (never weaken to pass, never change a check in the same unit as the
implementation it judges) is satisfied by construction. It strengthens the suite, so the assertion
ratchet (V-MET-003) rises rather than falls.

---

## Acceptance → check binding (07 §2 "Accept")

Every bullet binds to at least one check ID. No bullet is unbound.

| Accept                                                                                  | Check IDs                                          | Level      | Target                               |
| --------------------------------------------------------------------------------------- | -------------------------------------------------- | ---------- | ------------------------------------ |
| **(a)** clean-clone install brings all three tiers Ready, published images              | V-CMP-001, V-CMP-002, V-CMP-004, V-CMP-005         | L0, L2, L3 | kind + live (L3 partly deferred)     |
| **(b)** every tier completes an inference call and mints a token from its own namespace | V-CMP-001 (C5, C6 Wired+Exercised probes)          | L2, L3     | kind + live (L3 deferred)            |
| **(c)** empty **or blank** allowlist rejected at admission; no `*_ALLOW_ALL_USERS` env  | V-CTR-002 (V-7 negative), **V-CTR-014**            | L0, L2     | kind                                 |
| **(d)** off-allowlist egress blocked while Workload Identity still works                | V-CTN-020                                          | L2\*, L3   | **kind-kube-agents-egress** (Calico) |
| **(e)** the invariants gate reflects the imperative model                               | V-MET-003, V-MET-004, V-MET-006, V-MET-007         | L0         | none                                 |
| **(f)** `verify-phase7.sh` regression green                                             | the full prior ratchet (03 §11, 05 §8, phases 2–7) | L1, L2     | kind-kube-agents-dev                 |

\* **V-CTN-020 is BLOCKING-ALWAYS and may never be deferred** (09 §9.6). Its L2 instance runs on
`kind-kube-agents-egress` (Calico v3.28.0) — the **only** Kind target where an egress claim may be
green (LSN-006, `binding.md` P4). That cluster does **not currently exist** and P8-T2 must create it
from `local-dev/kind/kind-calico.yaml`. Its L3 instance on live GKE is a separately-recorded
carried deferral (see Deferrals below), consistent with how the scratch-GKE V-G checks are carried.

---

## Task breakdown

Units are ordered by dependency, then by risk. **P8-T1 ships first and alone if necessary** — 07 §2
says so explicitly, because it is an open hole on a running deployment.

| Task      | What to build                                                                                                                                                                                                                                                                                                                       | Spec                | Files                                                                                                                                                                                                     | Check IDs                                          | Weight           |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- | ---------------- |
| **P8-T1** | Close the `allowedUsers` bypass on **all four layers**: count **non-empty** entries in the webhook; CEL → `self.allowedUsers.exists(u, u.trim() != "")`; emit the list conditionally in all three templates and **fail fast** in provisioning when the variable is unset; **delete the `*_ALLOW_ALL_USERS` escape hatch entirely.** | 03 §4a, 06 §1.2 V-7 | `api/v1alpha1/agent_types.go:41,80` · `internal/webhook/agent_webhook.go:236-255` · `internal/controller/agent_manifests.go:446,465` · `scripts/*.template` ×3 · `provision_0{5,6,8}*.sh` · CRD + goldens | **V-CTR-002**, **V-CTR-014**, V-CTR-001            | **load-bearing** |
| **P8-T2** | Enforce egress. Stand up the Calico Kind target; **resolve the metadata-server conflict first** (169.254.169.254 is how WI mints tokens); render real CIDRs in place of `REPLACE_WITH_*`; apply the three per-tier policies from an install path.                                                                                   | 03 §9               | `local-dev/kind/kind-calico.yaml` · the three `netpol-*-egress.yaml` · the install path that applies them                                                                                                 | **V-CTN-020**, V-CMP-003                           | **load-bearing** |
| **P8-T3** | Apply the tenant isolation manifests provisioning currently skips: `ResourceQuota` and the namespace default-deny.                                                                                                                                                                                                                  | 03 §9               | `namespaces/team-x/10-resourcequota.yaml`, `20-netpol-default-deny.yaml` · provisioning                                                                                                                   | V-CMP-001 (Wired), V-CMP-003                       | medium           |
| **P8-T4** | Make a multi-tier install work: replace the headless-hanging `mcp-remote` OAuth bridge with a metadata-token stdio→HTTP bridge; give the dashboard container its rendered config and telemetry env; ship the `ExternalName` aliases. **Exercise with ≥2 agents in one namespace (LSN-015).**                                        | 05 §5               | `deploy/docker/Dockerfile:80-82` · `deploy/shared/defaults/config.yaml:17` · dashboard container spec · alias manifests · the `system-metadata` PVC naming                                                | V-CMP-001, V-CMP-004, V-CMP-005                    | high             |
| **P8-T5** | Image provenance: publish `kage-router`, `cluster-admin-agent`, `developer-team-agent`; make `:v0.1.0` real or repin every reference to a tag CI produces; add a Cloud Build path for operator and router; document the Apple-Silicon `PREBUILT_BINARY` escape hatch; **stop pushing `:latest`.**                                   | 05 §3               | `.github/workflows/docker-publish-ghcr.yml` · `Makefile:21,32` · every manifest referencing a first-party image                                                                                           | **V-CMP-002**                                      | high             |
| **P8-T6** | Make the invariants gate **mechanical**: a pre-merge script that fails any diff adding a write verb to an agent identity while broker/classifier/journal/undo are absent, and that flags a net reduction in security assertions.                                                                                                    | README, 07 §5       | new `local-dev/tests/invariants-gate.py` (or `.sh`) · `.claude/harness/invariants.md` · CI wiring                                                                                                         | **V-MET-003**, **V-MET-004**, V-MET-006, V-MET-007 | **load-bearing** |
| **P8-T7** | Documentation truth: remove the retired `PlatformAgent` kind (incl. the failing command at `INSTALL.md:276`); extend the pipeline list past step 11; fold in the live-install learnings; fix the Minty org prerequisite, the Slack App-Home DM checkbox, and the LiteLLM port-80 detail.                                            | —                   | `INSTALL.md:38,73,276,353,669` · `k8s-operator/scripts/README.md`                                                                                                                                         | V-MET-013 (doc-drift, single definition site)      | low              |
| **P8-T8** | Consolidated gate `local-dev/kind/verify-phase8.sh` + a live-target checklist; regression `verify-phase7.sh`.                                                                                                                                                                                                                       | 07 §5               | new `local-dev/kind/verify-phase8.sh`                                                                                                                                                                     | all of the above + **V-MET-007**                   | **load-bearing** |

---

## Deferrals opened by this phase (each with a named external blocker)

Recorded here at PLAN time so they are visible from the start rather than discovered at the gate.
None is a BLOCKING-ALWAYS check being hidden; V-CTN-020's blocking L2 instance **does** run.

| Check / bullet                      | Blocker (external, named)                                                                                                 | Owner | Promote when                                    |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ----- | ----------------------------------------------- |
| Accept (a)/(b) at **L3**            | No empty GCP project provisioned; a clean-clone install onto one needs billing, quota and credentials the harness lacks   | human | An empty scratch project exists                 |
| V-CTN-020 at **L3** (live GKE)      | The live cluster `platform-agent-host` has no Dataplane V2; enabling it is a cluster recreation on a non-ephemeral target | human | Dataplane V2 enabled, or a scratch GKE stood up |
| V-CMP-001 C5/C6 **Exercised** at L3 | Same as above — needs the live multi-tier install                                                                         | human | The live install is rebuilt on the fixed path   |

---

## Notes carried into IMPLEMENT

- **Precondition P1 is mandatory before any L2 judgement**: rebuild → `kind load` → `rollout
restart` → **assert the running `imageID` digest matches the build under test**. LSN-001 recurred
  three times; a same-tag image is not evidence.
- **P3**: admission policies do not evict what already exists. Force-recreate the pods the property
  is about, or the check is testing the past.
- **P4**: an egress claim is green only on Calico/Dataplane V2. On kindnet it is `deferred`, never
  `pass`.
- **P6 / LSN-003 (open)**: assert against the **operator-rendered ConfigMap**, not the image-baked
  `config.yaml` it shadows, and name which one the check reads.
- **LSN-015 (open)**: any per-agent resource is exercised with **≥2 agents in one namespace**.
  P8-T4 is where this binds.
- The destructive-test guard (`binding.md` P5) is an **anchored** `case` on `kind-*` /
  `gke-scratch-*`. The live cluster `platform-agent-host` is **not** a destructive-test target.
