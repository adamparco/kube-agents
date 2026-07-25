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

**Resolved in P8-T2 (2026-07-25).** Two things this finding did not know about turned up while fixing
it, both worse than what it describes:

1. **The shipped remediation advice was wrong.** All three policies carried a note recommending a
   narrow allow to `169.254.169.254/32:988`. That is not a pairing that exists: 988/987 belong to
   `169.254.169.252/32` on Dataplane V1 / Calico, and `169.254.169.254/32` takes 80/8080 on Dataplane
   V2. Following the shipped advice produces a policy that silently breaks Workload Identity — and it
   fails as a **timeout inside the auth client library**, an authentication error that never mentions
   the network. A remediation note that does not work is worse than no note, because it gets followed
   without testing.
2. **The platform and cluster-admin policies had no rule for the in-cluster LiteLLM/minter hop at
   all.** Their rules 2 and 3 were remote-hub `REPLACE_WITH_*` ipBlocks at :443. Default-deny egress
   governs same-namespace traffic too — being in `kubeagents-system` does not exempt a pod from a
   policy that selects it — so had these ever been applied, both tiers would have lost inference. The
   finding above says the policies are "structurally correct"; they were not.

The resolution is absent-by-default: no metadata address in the base allowlist (unreachable **by
omission**, not by a deny rule a later edit could reorder past), rendered back narrowly and port-bound
only when `WORKLOAD_IDENTITY_ENABLED=true`, with **both** dataplane pairings emitted under
`GKE_DATAPLANE=auto`. That makes the conjunction checkable rather than asserted: the metadata rule is
present iff WI is enabled, and **port narrowing is provably enforced** on Calico (same namespace,
allowed port reachable, other port denied) — which is the property that makes a narrow metadata allow
meaningfully narrower than a whole-host one. The live-WI half stays an L3 deferral with a named
blocker; it is not claimed.

### Tenant isolation manifests are written but never applied (P8-T3)

`examples/gitops-repo/clusters/cluster-a/namespaces/team-x/10-resourcequota.yaml` and
`20-netpol-default-deny.yaml` exist; provisioning skips both.

**Resolved in P8-T3 (2026-07-25)** — and the finding understated the problem in one direction while
being precisely right in the other.

Right: both manifests were dead. They are now rendered from `tenant-quota.yaml.template` and
`netpol-tenant-default-deny.yaml.template` and applied by the install path, with the two controls
deliberately split across steps because their ordering constraints are opposite — the quota lands in
`provision_12` **before** the agent pod it governs, the floor lands in `provision_13` **after** the
pod is Ready and after the per-tier allowlist. Applying either at the other's point breaks the
install: a quota applied after the pod leaves a trap for the next rollout, and a floor applied before
the pod is Ready blackholes DNS and deadlocks `wait_for_k8s_resource … Available` on a 300s timeout
that never mentions NetworkPolicy.

Understated: the finding named two orphaned manifests. The install path had a third and worse
orphan — **`provision.sh` stopped at step 12**, so `provision_13_apply_network_policies.sh`, written
in P8-T2 to apply the three per-tier egress policies, was invoked by nothing and had no teardown.
P8-T2's own ledger row claims those policies are "applied from an install path". They were not.

That is **LSN-007 recurring inside the unit that was fixing LSN-006**, and the reason it could recur
is that LSN-007 had been closed by naming the check ID `V-CMP-001` — which no script implemented.
`local-dev/tests/install-path-wired.py` now implements it generically (every numbered step is invoked
by its driver; every driver reference resolves; provision/teardown are symmetric; teardown descends),
and it found both defects on its first run. The generalization is **LSN-019**, opened and bound to
P8-T6: a lesson whose Mechanization field names a check ID rather than a runnable artifact is not
closed, and several current closures will not survive that test.

### A multi-tier install does not work (P8-T4)

- The `mcp-remote` OAuth bridge (`deploy/docker/Dockerfile:80-82`, wired at
  `deploy/shared/defaults/config.yaml:17`) **hangs headless** — it wants an interactive OAuth flow.
- The dashboard container lacks the rendered config and telemetry env the other containers get.
- The `ExternalName` aliases a developer-team agent needs for in-namespace `litellm` /
  `github-token-minter` DNS are asserted by `local-dev/kind/verify-phase3.sh:151-154` but not
  shipped by any install path.
- **LSN-015 (closed here).** The first real multi-tier install deadlocked because the
  `system-metadata` PVC is a fixed namespace-scoped name with `ReadWriteOnce` while the data PVC is
  per-agent — so the **second** agent in a namespace hangs in `ContainerCreating`. Every prior
  fixture ran exactly one agent per namespace and could not see it.
  `local-dev/kind/multi-agent-namespace-l2.sh` now runs two, and the lesson recurred one level up
  while writing it: `ReadWriteOnce` excludes per **node**, so a single-node cluster cannot exhibit
  the multi-attach either. The symptom half of the fixture therefore **defers with measured
  numbers** rather than passing — the naming half proves the defect is gone by construction, since
  no two pods reference one claim.

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

## Second planning defect, found during P8-T1: V-CTR-002 is wider than P8-T1

The table below originally bound P8-T1 to **V-CTR-002** without qualification. That binding is
wrong, and the error only became visible while implementing the unit.

V-CTR-002's full scope is _"each of **V-1…V-10** has a negative test rejected with the field path in
the message"_ (¬, at L2). P8-T1 is the closed-allowlist rule — **V-7 only**. Three of the ten rules
V-CTR-002 covers are **not implemented in the webhook at all**:

| Rule     | 06 §1.2                                    | Status                                                |
| -------- | ------------------------------------------ | ----------------------------------------------------- |
| **V-6**  | Cross-object ceiling (child RBAC ⊆ parent) | Not implemented — carried as 08 §5 deferred hardening |
| **V-8**  | Budget clamp                               | Not implemented                                       |
| **V-10** | Reader-only ServiceAccount override        | Not implemented                                       |

Delivering all of V-CTR-002 therefore means writing three new admission rules plus a ten-case L2
negative-test suite — far beyond "close the `allowedUsers` bypass", and a violation of the
one-bounded-unit rule (PROTOCOL §3) if smuggled into P8-T1.

**Resolution — split, do not shrink.** P8-T1 completes with the **V-7 slice** of V-CTR-002 proven at
L0 and L2; V-CTR-002 is recorded in the ledger as **partial** with the gap named (V-6/V-8/V-10 have
no rule to negatively test). The remainder becomes a new unit, **P8-T9**, which implements V-6, V-8
and V-10 and builds the full V-1…V-10 L2 negative-test suite. V-CTR-002 is **BLOCKING-PHASE for
phase 8** (09 §6.9), not BLOCKING-ALWAYS, so a named-blocker partial is legitimate _within_ the
phase — but it must be fully green at MILESTONE, which is what P8-T9 exists to guarantee. This
narrows a **claim**, never a check: no assertion is removed, and the total rises by P8-T9's suite.

---

## Acceptance → check binding (07 §2 "Accept")

Every bullet binds to at least one check ID. No bullet is unbound.

| Accept                                                                                  | Check IDs                                                        | Level      | Target                               |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ---------- | ------------------------------------ |
| **(a)** clean-clone install brings all three tiers Ready, published images              | V-CMP-001, V-CMP-002, V-CMP-004, V-CMP-005                       | L0, L2, L3 | kind + live (L3 partly deferred)     |
| **(b)** every tier completes an inference call and mints a token from its own namespace | V-CMP-001 (C5, C6 Wired+Exercised probes)                        | L2, L3     | kind + live (L3 deferred)            |
| **(c)** empty **or blank** allowlist rejected at admission; no `*_ALLOW_ALL_USERS` env  | V-CTR-002 (V-7 slice in P8-T1; V-1…V-10 in P8-T9), **V-CTR-014** | L0, L2     | kind                                 |
| **(d)** off-allowlist egress blocked while Workload Identity still works                | V-CTN-020                                                        | L2\*, L3   | **kind-kube-agents-egress** (Calico) |
| **(e)** the invariants gate reflects the imperative model                               | V-MET-003, V-MET-004, V-MET-006, V-MET-007                       | L0         | none                                 |
| **(f)** `verify-phase7.sh` regression green                                             | the full prior ratchet (03 §11, 05 §8, phases 2–7)               | L1, L2     | kind-kube-agents-dev                 |

\* **V-CTN-020 is BLOCKING-ALWAYS and may never be deferred** (09 §9.6). Its L2 instance runs on
`kind-kube-agents-egress` (Calico v3.28.0) — the **only** Kind target where an egress claim may be
green (LSN-006, `binding.md` P4). That cluster does **not currently exist** and P8-T2 must create it
from `local-dev/kind/kind-calico.yaml`. Its L3 instance on live GKE is a separately-recorded
carried deferral (see Deferrals below), consistent with how the scratch-GKE V-G checks are carried.

---

## Task breakdown

Units are ordered by dependency, then by risk. **P8-T1 ships first and alone if necessary** — 07 §2
says so explicitly, because it is an open hole on a running deployment.

| Task        | What to build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Spec                | Files                                                                                                                                                                                                                                                                                                                                                                  | Check IDs                                                | Weight           |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------- |
| **P8-T1**   | Close the `allowedUsers` bypass on **all four layers**: count **non-empty** entries in the webhook; CEL → an `exists(u, non-blank)` predicate; emit the list conditionally in all three templates and **fail fast** in provisioning when the variable is unset; **delete the `*_ALLOW_ALL_USERS` escape hatch entirely.**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 03 §4a, 06 §1.2 V-7 | `api/v1alpha1/agent_types.go:41,80` · `internal/webhook/agent_webhook.go:236-255` · `internal/controller/agent_manifests.go:446,465` · `scripts/*.template` ×3 · `provision_0{5,6,8}*.sh` · CRD + goldens                                                                                                                                                              | **V-CTR-002 (V-7 slice only)**, **V-CTR-014**, V-CTR-001 | **load-bearing** |
| **P8-T2** ☑ | Enforce egress. Stand up the Calico Kind target; **resolve the metadata-server conflict first** (169.254.169.254 is how WI mints tokens); render real CIDRs in place of `REPLACE_WITH_*`; apply the three per-tier policies from an install path. **Done:** `local-dev/kind/up-egress.sh` (Calico v3.28.0) · one `netpol-agent-egress.yaml.template` + three `common.sh` render helpers · `provision_13_apply_egress_policies.sh` · exemplars regenerated as a derived artifact. **V-CTN-020 green (L0+L2, 18/18); V-CMP-003 partial** (21/22, 1 deferred on absent Config Connector CRDs).                                                                                                                                                                                                                                                                                                                                          | 03 §9               | `local-dev/kind/up-egress.sh` · `netpol-agent-egress.yaml.template` · `common.sh` · `provision_13_apply_egress_policies.sh` · the three `netpol-*-egress.yaml` · `local-dev/tests/egress-policy-render.py` · `local-dev/kind/egress-enforcement-l2.sh` · `local-dev/kind/gitops-tree-applies-l2.sh`                                                                    | **V-CTN-020**, V-CMP-003                                 | **load-bearing** |
| **P8-T3** ☑ | Apply the tenant isolation manifests provisioning currently skips: `ResourceQuota` and the namespace default-deny. **Done:** both rendered from new templates and applied from the install path — quota in `provision_12` before the agent pod, floor in `provision_13` after the pod and after the allowlist. **Found and fixed en route:** `provision.sh` never invoked `provision_13` (shipped P8-T2) and no `teardown_13` existed — LSN-007 recurring; `install-path-wired.py` now catches the class. Step 13 and the render golden renamed to match their scope; reference-render extended from 3 exemplars to 5. **V-CMP-001 green (L0+L2, 18/18 on Calico).**                                                                                                                                                                                                                                                                 | 03 §3, §9, §10      | `tenant-quota.yaml.template` · `netpol-tenant-default-deny.yaml.template` · `common.sh` (3 helpers) · `provision_12` · `provision_13_apply_network_policies.sh` · `teardown_13_apply_network_policies.sh` · `provision.sh` · `teardown.sh` · `local-dev/tests/install-path-wired.py` · `local-dev/tests/reference-render.py` · `local-dev/kind/tenant-isolation-l2.sh` | **V-CMP-001**, V-CMP-003                                 | **load-bearing** |
| **P8-T4** ☑ | Make a multi-tier install work. **Done:** the `mcp-remote` OAuth bridge is replaced by `mcp_http_bridge.py` (guarded by `test_mcp_bridge.py`); the dashboard container gets the rendered config + 4 OTEL vars; the `ExternalName` aliases render from `tenant-service-aliases.yaml.template` and are applied by `provision_12` **before** the CR. The skill's third copy of all four isolation manifests is now byte-bound to the installer render (`test_skill_templates.py`, 8 tests, 10/10 mutations caught). **LSN-015 closed** by `multi-agent-namespace-l2.sh`: two agents, one namespace, distinct per-agent claims — CLAIM 2 (both pods Ready) deferred, needs 2 nodes + 6Gi. **Also found and fixed:** `config/router/deployment.yaml` shipped `REPLACE_WITH_PROJECT_ID` to a running pod (V-CMP-003) — 414 CrashLoopBackOff restarts on the dev cluster, failing as a _credentials_ error that never named the real cause. | 05 §5               | `deploy/docker/Dockerfile:80-82` · `deploy/shared/defaults/config.yaml:17` · dashboard container spec · alias manifests · the `system-metadata` PVC naming                                                                                                                                                                                                             | V-CMP-001, V-CMP-004, V-CMP-005                          | high             |
| **P8-T5**   | Image provenance: publish `kage-router`, `cluster-admin-agent`, `developer-team-agent`; make `:v0.1.0` real or repin every reference to a tag CI produces; add a Cloud Build path for operator and router; document the Apple-Silicon `PREBUILT_BINARY` escape hatch; **stop pushing `:latest`.**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 05 §3               | `.github/workflows/docker-publish-ghcr.yml` · `Makefile:21,32` · every manifest referencing a first-party image                                                                                                                                                                                                                                                        | **V-CMP-002**                                            | high             |
| **P8-T6**   | Make the invariants gate **mechanical**: a pre-merge script that fails any diff adding a write verb to an agent identity while broker/classifier/journal/undo are absent, and that flags a net reduction in security assertions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | README, 07 §5       | new `local-dev/tests/invariants-gate.py` (or `.sh`) · `.claude/harness/invariants.md` · CI wiring                                                                                                                                                                                                                                                                      | **V-MET-003**, **V-MET-004**, V-MET-006, V-MET-007       | **load-bearing** |
| **P8-T7**   | Documentation truth: remove the retired `PlatformAgent` kind (incl. the failing command at `INSTALL.md:276`); extend the pipeline list past step 11; fold in the live-install learnings; fix the Minty org prerequisite, the Slack App-Home DM checkbox, and the LiteLLM port-80 detail. **Add (found in P8-T4):** the `gke` MCP server is documented in four places and shipped in none — `config.md:32` quotes the removed `proxy.js` inside a copy-pasteable config block, and `config.md:69`, `platform-agent.md:30`, `what-is-kube-agents.md:22`, `install/manual.md:68` all describe it as present. Fix as one set, not piecemeal.                                                                                                                                                                                                                                                                                             | —                   | `INSTALL.md:38,73,276,353,669` · `k8s-operator/scripts/README.md`                                                                                                                                                                                                                                                                                                      | V-MET-013 (doc-drift, single definition site)            | low              |
| **P8-T8**   | Consolidated gate `local-dev/kind/verify-phase8.sh` + a live-target checklist; regression `verify-phase7.sh`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | 07 §5               | new `local-dev/kind/verify-phase8.sh`                                                                                                                                                                                                                                                                                                                                  | all of the above + **V-MET-007**                         | **load-bearing** |
| **P8-T9**   | Finish **V-CTR-002**: implement the three unimplemented 06 §1.2 rules — **V-6** cross-object ceiling (child RBAC ⊆ parent), **V-8** budget clamp, **V-10** reader-only ServiceAccount override — and build the full **V-1…V-10** L2 negative-test suite, one rejected fixture per rule, each denial asserted to name its field path.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 06 §1.2, 08 §5      | `internal/webhook/agent_webhook.go` · `api/v1alpha1/*_types.go` (V-8 bounds) · new `local-dev/kind/webhook-negatives-l2.sh` · CRD + goldens                                                                                                                                                                                                                            | **V-CTR-002** (completes it)                             | **load-bearing** |

**P8-T9 must land before MILESTONE.** V-CTR-002 is BLOCKING-PHASE for phase 8: P8-T1 leaves it
`partial` with a named gap, and the phase gate does not pass while it stays that way. It is
sequenced last among the containment units because V-6 is the 08 §5 cross-object webhook, whose
blast radius is wider than the rest of the phase combined.

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
