# SOP: Fleet Consistency Drift Audit (Weekly Governance)

**Purpose:** Find the cluster that does not match its peers. For each configuration facet, compute what the majority of comparable clusters do, then report the outliers — answering "eleven of my twelve clusters have Workload Identity on; which one doesn't, and why didn't I know?"

**Data sources:** The baseline is **derived from the live fleet and nowhere else.** `gcloud container clusters list/describe --format=json`, `gcloud container node-pools list/describe --format=json`, read-only `kubectl`, the `gke` MCP server, and the platform MCP tools (`list_cc_pods`, `get_cc_pod_diagnostics`, `list_cc_healthchecks`, `get_cc_operator_status`). There is no Platform Master Blueprint, no standards document, no CMDB, no Terraform state, no Config Sync repo, no BigQuery, no Prometheus. If you find yourself wanting an "expected value" from outside the fleet, you have left this SOP.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_pr.py start --audit fleet-consistency-drift
```

Returns `{"branch":…, "existing_pr":…, "repo":…, "findings_path":"/opt/data/scratch/findings_fleet-consistency-drift.json"}`. Use the returned `findings_path` verbatim; you are now on the audit branch. Do not create branches, commit, push, or call `gh` yourself — the helper owns every git and GitHub operation and renders the PR body. You never hand-write a PR body.

### 1. Enumerate the target fleet

1. Resolve the project set: `gcloud config get-value project`, plus any project IDs already recorded in `/opt/data/INVENTORY.md`. `INVENTORY.md` supplies **project IDs only** — never expected values.
2. Per project, enumerate with `gcloud container clusters list --project <proj> --format=json`, which returns full Cluster resources.
3. For every enumerated cluster capture the authoritative JSON with `gcloud container clusters describe <name> --location <loc> --project <proj> --format=json`. That literal invocation, with real values, is the `evidence.command` of every finding about that cluster. Never record a command you did not run.
4. `scope.clusters` is **every cluster enumerated**, compared or not — the harness rejects an empty list. If **zero** clusters enumerate, do not call `finish`: a fleet you could not read is not a clean fleet, so report the enumeration failure as your one-line summary and stop rather than returning `[SILENT]`. `scope.skipped` is the subset excluded from comparison, each with a reason:
   - `status` is not `RUNNING` (`PROVISIONING`, `RECONCILING`, `STOPPING`, `ERROR`, `DEGRADED`) — a cluster mid-change is not drifting.
   - `createTime` is under 24 hours old — a brand-new cluster has not settled.
   - `describe` failed or was denied — quote the error in the reason.
   - Its cohort is below the §2 floor.

### 2. Build comparability cohorts

A dev cluster diverging from a prod cluster is intent, not drift. Group before comparing.

1. **Mode** (always part of the key): `autopilot` when `.autopilot.enabled == true`, else `standard`. Autopilot and Standard clusters are **never** comparable for node-level facets and are kept in separate cohorts throughout.
2. **Environment signal**, resolved in this fixed order: the first present of `.resourceLabels.environment`, `.env`, `.stage`, `.tier`, lowercased; otherwise a token match in the cluster name split on `-`/`_` against `{prod, prd, production, staging, stg, stage, preprod, dev, development, sandbox, sbx, test, qa, uat}`, which yields an **inferred** environment and costs a severity step later; otherwise `unknown`. Normalize synonyms: `prod|prd|production → prod`, `staging|stg|stage|preprod → staging`, `dev|development|sandbox|sbx → dev`, `test|qa|uat → test`. Any other literal keeps its own value.
3. **Cohort key:** if any cluster in the fleet has a non-`unknown` environment → `(mode, environment)`, and the `unknown` clusters form their own cohort, never merged into a named one. Else if the fleet spans more than one project → `(mode, project)`, using the project as the environment proxy. Else → `(mode)` alone.
4. **Minimum cohort size — the floor.** A cohort of fewer than **3** clusters produces no findings, ever; two clusters disagreeing is a coin flip, not a majority. Record every member of an undersized cohort in `scope.skipped` with reason `cohort <key> has only N comparable clusters (minimum 3)`. If no cohort reaches 3, the run emits zero findings and still completes §6 and §7.

### 3. Derive the baseline

For each cohort `C` (size ≥ 3) and each facet `F`:

1. Normalize every member's raw value to one comparable token per the facet's rule. A member whose value cannot be read (field absent fleet-wide, API error, permission denied) is `UNREADABLE`: excluded from the vote, and never an outlier for that facet.
2. Let `n` = voting members, `m` = count of the most common token `t*`, `r = m / n`, `k = n - m` outliers.
3. **A baseline exists only when `n >= 3` and `r >= 2/3`.** Otherwise there is no baseline and no finding — reporting a 50/50 or a 4/7 split as drift is noise. (A tie for first place cannot reach `r >= 2/3`, so uniqueness of `t*` follows.)
4. Every member whose token differs from `t*` is an outlier and yields one finding.
5. **Confidence to severity.** Start at the facet's base severity and walk down the ladder `critical > major > minor`: `r < 0.90` → one step; `r < 0.80` → one further step (cumulative); `k >= 3` → one step (three-plus divergent clusters is an undeclared cohort, not an outlier); the outlier's or the baseline's cohort membership rests on an **inferred** environment → one step. If the result falls below `minor`, **drop the finding.** A base-`major` facet at `r = 0.71` therefore disappears while a base-`critical` facet at `r = 0.71` survives as `minor`. That is intended: a weak majority only earns an admin's attention when the stake is high.
6. **Split-cluster guard.** If one cluster is an outlier on **6 or more** facets it is not drifting, it is a different kind of cluster. Suppress its individual facet findings and emit one `major` finding `fcd-uncohorted-<cluster-slug>` naming them, so the admin fixes the cohort labelling instead of twelve symptoms.
7. **Ids must be stable across runs:** `fcd-<facet-slug>-<project>-<location>-<name>`, lowercased, non-alphanumerics collapsed to `-`. Never put counts, ratios, dates, or severities in an id — the harness's new/resolved delta depends on the same drift keeping the same id between runs.
8. **Every finding shows its work.** `evidence.excerpt` opens with these four labelled lines — the harness clips excerpts at 40 lines / 2000 characters, so they go first and the raw JSON fragment follows. Without them the audit reads as an oracle and gets ignored:

```
baseline: <field path>=<t*> in <m>/<n> clusters of cohort <mode>/<env>
peers: <up to 6 cluster names>, +<N> more
observed: <token>  (<raw JSON fragment or "key absent">)
consensus: <r to 2dp> -> severity <sev> (base <base>, <downgrades applied or "none">)
```

### 4. Facet comparison

#### 4.0 Rules that apply to every facet

- **Field-path discipline.** Confirm a facet's path exists in at least one cluster's real `--format=json` output before comparing it. If it is absent from every cluster in the cohort the facet is `UNREADABLE` fleet-wide — skip it silently; never emit a finding asserting the whole fleet is missing a field you could not locate. Where two paths are plausible (a field that migrated between API versions), read the first present and record which one in the excerpt.
- **Absent, empty, and `false` are one token.** Proto3 omits false booleans and empty messages, so a missing `shieldedNodes` key, `{"shieldedNodes":{}}`, and `{"enabled":false}` all normalize to `OFF`. A serialization artefact must never become a finding.
- **Baseline** is the plain §3 majority over the cohort unless a facet says otherwise.
- **Global suppressions — do NOT flag** (assumed everywhere below, not repeated): clusters skipped in §1; clusters in a different cohort; Autopilot clusters on any node-level facet; `UNREADABLE` values; cohorts under the §2 floor; facets with no §3 baseline.

#### 4.1 Release channel

- **Read:** `.releaseChannel.channel` → `RAPID`/`REGULAR`/`STABLE`; absent, `{}`, or `UNSPECIFIED` → `NONE`.
- **Do NOT flag:** a `NONE` cluster pinned to a specific `currentMasterVersion` for a documented dependency — check `.resourceLabels` for a pin/freeze marker the fleet uses elsewhere first.
- **Severity:** base `major` for `NONE` against a channelled majority; base `minor` for a mismatch between two real channels.
- **Impact:** the outlier receives security patches on a different schedule than every peer.
- **Remediation:** `gcloud` — `gcloud container clusters update <name> --location <loc> --project <proj> --release-channel=<t*>` (`# leaving NONE for a channel is one-way`).

#### 4.2 Workload Identity

- **Read:** `.workloadIdentityConfig.workloadPool` → non-empty string `ON`, absent or empty `OFF`. Compare on/off only; the pool string embeds the project id and legitimately differs.
- **Do NOT flag:** nothing beyond §4.0 — no cluster has a good reason to be the only one in its cohort on node service-account credentials.
- **Severity:** base `critical`.
- **Impact:** the outlier's pods reach Google APIs with node-level credentials, so every pod on a node inherits that node's IAM.
- **Remediation:** `gcloud` — `gcloud container clusters update … --workload-pool=<proj>.svc.id.goog` (`# node pools must then be updated or recreated to expose the metadata server`).

#### 4.3 Shielded Nodes, secure boot, integrity monitoring

- **Read:** three facets. Cluster-level `.shieldedNodes.enabled` → `ON`/`OFF`. Per-pool `.nodePools[].config.shieldedInstanceConfig.enableSecureBoot` and `.enableIntegrityMonitoring` → `ALL` (every pool true), `SOME` (mixed), `NONE` (no pool true, or absent on all pools). Standard cohorts only for the two per-pool facets.
- **Do NOT flag:** pools whose `.config.imageType` cannot support secure boot (Windows and some third-party images) — exclude those pools from the fraction rather than letting them manufacture a `SOME`; a cluster whose only divergent pool is a burst or spot pool created in the last 24 hours.
- **Severity:** base `major` for Shielded Nodes and secure boot, base `minor` for integrity monitoring.
- **Impact:** nodes boot unverified where every peer verifies them.
- **Remediation:** `gcloud` for the cluster flag — `gcloud container clusters update … --enable-shielded-nodes`. Per-pool secure boot cannot be toggled in place, so those are `manual`: recreate the pool with `--shielded-secure-boot`.

#### 4.4 Network policy enforcement

- **Read:** if `.networkConfig.datapathProvider == ADVANCED_DATAPATH` → `DPV2` (Dataplane V2 enforces natively and the Calico fields are meaningless there); else if `.networkPolicy.enabled == true` and `.addonsConfig.networkPolicyConfig.disabled` is not true → `CALICO`; else `OFF`. Confirm both Calico paths in the real JSON before relying on either.
- **Do NOT flag:** `DPV2` against a `CALICO` majority or the reverse — two implementations of one control. Emit a finding only when the outlier is `OFF`.
- **Severity:** base `major`.
- **Impact:** pod-to-pod traffic is unrestricted in the outlier where peers segment it.
- **Remediation:** `gcloud` — `gcloud container clusters update … --enable-network-policy` (`# restarts the cluster networking add-ons`).

#### 4.5 Private nodes, private endpoint, authorized networks

- **Read:** three facets, each `ON`/`OFF`, from `.privateClusterConfig.enablePrivateNodes`, `.privateClusterConfig.enablePrivateEndpoint`, and `.masterAuthorizedNetworksConfig.enabled` plus `.cidrBlocks` (authorized networks is `ON` only when enabled **and** `cidrBlocks` is non-empty). Recent GKE versions carry equivalents under `.networkConfig` and `.controlPlaneEndpointsConfig`: read whichever is actually present and name the path in the excerpt.
- **Do NOT flag:** the **contents** of `cidrBlocks` — CIDRs legitimately differ per cluster and comparing them is guaranteed noise; `enablePrivateEndpoint: false` when the majority is also false (normal for admin-reachable control planes).
- **Severity:** base `critical` for private nodes and authorized networks, base `major` for private endpoint.
- **Impact:** the outlier exposes node or control-plane surface its peers keep private.
- **Remediation:** authorized networks are `gcloud` — `gcloud container clusters update … --enable-master-authorized-networks --master-authorized-networks=<ranges the cluster's own owner approves>`; never copy a peer's CIDRs. Private nodes usually cannot be enabled in place: prefer `manual` with the migration note unless `--enable-private-nodes` is valid for that cluster's version.

#### 4.6 Logging and monitoring component sets

- **Read:** three facets. `.loggingConfig.componentConfig.enableComponents[]` and `.monitoringConfig.componentConfig.enableComponents[]` are list-valued: deduplicate, sort ascending lexicographically, join with `,` — that canonical ordering is what makes them comparable, and an absent config, an absent `enableComponents`, and an empty list all become `NONE`. `.monitoringConfig.managedPrometheusConfig.enabled` → `ON`/`OFF`.
- **Do NOT flag:** a cluster whose set is a strict **superset** of the baseline — collecting more telemetry than peers is not drift. Flag subsets and disjoint sets, and name the missing components.
- **Severity:** base `major` when the outlier is missing `SYSTEM_COMPONENTS`, otherwise base `minor`.
- **Impact:** the outlier is invisible to fleet dashboards and alerts built on the peers' component set.
- **Remediation:** `gcloud` — `gcloud container clusters update … --logging=<t* comma list> --monitoring=<t* comma list>`.

#### 4.7 Binary Authorization

- **Read:** `.binaryAuthorization.evaluationMode`, falling back to legacy `.binaryAuthorization.enabled` when absent. `DISABLED`, `EVALUATION_MODE_UNSPECIFIED`, an absent block, and legacy `enabled: false` all → `OFF`.
- **Do NOT flag:** mode differences among enabled clusters — the policy content lives outside the cluster and is unreadable here, so only `OFF` against an enabled majority is a finding.
- **Severity:** base `major`.
- **Impact:** unsigned or unattested images can run on the outlier.
- **Remediation:** `gcloud` — `gcloud container clusters update … --binauthz-evaluation-mode=<t*>` (`# the project policy must already admit this cluster's workloads or deployments will be blocked`).

#### 4.8 Cluster autoscaling and node auto-provisioning

- **Read:** two facets, Standard cohorts only. `.autoscaling.enableNodeAutoprovisioning` → `ON`/`OFF`; `.nodePools[].autoscaling.enabled` → `ALL`/`SOME`/`NONE` as in §4.3.
- **Do NOT flag:** single-pool clusters against multi-pool peers on the `ALL`/`SOME`/`NONE` facet; pools carrying `.config.taints` that mark them dedicated or pinned capacity — exclude those from the fraction, they are deliberately fixed-size.
- **Severity:** base `minor`.
- **Impact:** the outlier cannot absorb load the way its peers do and needs manual capacity intervention.
- **Remediation:** `gcloud` — `gcloud container node-pools update <pool> --enable-autoscaling --min-nodes=<N> --max-nodes=<N>` or `clusters update … --enable-autoprovisioning …`; the limits are a human judgement, so leave them as named placeholders in a `#` comment rather than inventing numbers.

#### 4.9 Intra-node visibility and dataplane provider

- **Read:** two facets. `.networkConfig.enableIntraNodeVisibility` → `ON`/`OFF`; `.networkConfig.datapathProvider` → `ADVANCED_DATAPATH`, or `LEGACY_DATAPATH` for both the explicit value and an absent field.
- **Do NOT flag:** the dataplane facet on Autopilot cohorts — Autopilot is always Dataplane V2, so variation there is a read error, not drift.
- **Severity:** base `minor` for intra-node visibility, base `major` for dataplane provider.
- **Impact:** the outlier emits different flow telemetry and enforces network policy through a different engine than its cohort.
- **Remediation:** intra-node visibility is `gcloud` — `gcloud container clusters update … --enable-intra-node-visibility`. Dataplane V2 cannot be enabled on an existing cluster, so that one is `manual`: cluster recreation and workload migration.

#### 4.10 Maintenance window

- **Read:** `.maintenancePolicy.window.dailyMaintenanceWindow` and `.recurringWindow` → `DAILY`, `RECURRING`, or `NONE`. **Presence and kind only.** Start times are deliberately excluded: a window at 02:00 local in `us-east4` is a business-hours window in `asia-northeast1`, so comparing UTC start times across a multi-region cohort manufactures findings. The narrowing is the point.
- **Do NOT flag:** `DAILY` against `RECURRING` — only `NONE` against a configured majority is a finding.
- **Severity:** base `minor`.
- **Impact:** GKE can upgrade the outlier at any hour while its peers are protected.
- **Remediation:** `gcloud` — `gcloud container clusters update … --maintenance-window-start=… --maintenance-window-end=… --maintenance-window-recurrence=…`, times left to the cluster's owner.

#### 4.11 Resource label key set

- **Read:** `.resourceLabels`; token is the sorted set of **keys** joined with `,`, dropping keys prefixed `goog` (GKE writes those itself). Absent map and empty map both → `NONE`.
- **Baseline:** the majority key set. The expected keys are whatever the cohort demonstrably carries — **do not invent label keys** such as `owner` or `cost-center` because they sound standard.
- **Do NOT flag:** label **values** (they vary by design, including the environment label used for cohorting); a cluster carrying extra keys beyond the baseline; any key held by fewer clusters than the §3 threshold.
- **Severity:** base `minor`.
- **Impact:** the outlier drops out of cost attribution and label-scoped queries its peers appear in.
- **Remediation:** `gcloud` — `gcloud container clusters update … --update-labels=<key>=<VALUE>` (`# VALUE must be supplied by the cluster owner`).

#### 4.12 Node image type

- **Read:** `.nodePools[].config.imageType`; token is the sorted set of distinct image types joined with `,`. Standard cohorts only.
- **Do NOT flag:** Windows pools in an otherwise Linux cluster — a deliberate workload requirement, visible as a `WINDOWS_*` image type, so exclude those pools from the set; a difference that is only a `_CONTAINERD` suffix on the same base family, which is a rename rather than a divergence.
- **Severity:** base `minor`.
- **Impact:** the outlier's nodes carry a different patch cadence, kernel, and hardening baseline.
- **Remediation:** `manual` — image type cannot be changed in place; recreate the pool with `--image-type=<t*>`.

#### 4.13 Database encryption (etcd CMEK)

- **Read:** `.databaseEncryption.state` → `ENCRYPTED`/`DECRYPTED`, with an absent block `DECRYPTED`. Compare the state only — **never `keyName`**, which is region- and project-scoped and legitimately differs.
- **Do NOT flag:** `ENCRYPTED` with an unreachable key — that is a health problem for a different audit, not consistency drift.
- **Severity:** base `critical`.
- **Impact:** application secrets in the outlier's etcd are not wrapped with the customer-managed key every peer uses.
- **Remediation:** `gcloud` — `gcloud container clusters update … --database-encryption-key=<KEY>` (`# KEY must be created in the cluster's region and IAM-bound by a human first; do not reuse a peer's key`).

### 5. Generate remediation artifacts

- These are control-plane settings, so `kind` is almost always `gcloud` or `manual`. For `gcloud` the harness renders `remediation.note` **inside a bash code block**, so the note must be shell-pasteable: the command, with any caveat as a `#` comment. For `manual` the note is prose — say plainly that the change needs pool or cluster recreation rather than emitting a command that would fail.
- `remediation.path` is **only permitted when `kind == "manifest"`**; a path on a `gcloud` or `manual` remediation is a hard validation failure. Use `manifest` only when the fix is a genuine in-cluster object, write the file to `remediations/fleet-consistency-drift/<file>.yaml` in the working tree **before** `finish`, and give that repo-relative path — the harness stages exactly those files and errors if one is missing.
- Never copy cluster-scoped values from a peer (CIDRs, KMS key names, label values, autoscaling limits). Leave a named placeholder and say what the human supplies.
- Mark disruptive remediations in the note: anything that recreates node pools, restarts networking add-ons, or is one-way.

### 6. Emit findings.json

Write the document to the `findings_path` from §0, with `"audit": "fleet-consistency-drift"`.

- `scope.clusters` — every enumerated cluster with `name`, `location`, `project`; non-empty or the run fails.
- `scope.skipped` — every excluded cluster with its §1/§2 reason (`[]` when nothing was skipped).
- Per finding: `namespace` is `""` and `object` is `Cluster/<name>` for cluster-scoped facets; `id` per §3.7 and unique within the file; `severity` one of `critical`/`major`/`minor`.
- `title` names the facet and the divergence and carries **no counts** — the harness renders the body deterministically, so a stable title keeps an unchanged fleet byte-identical between runs. Counts belong in the excerpt.
- `evidence.command` is **mandatory and must be the literal command you ran** — the §1.3 `describe` invocation with real values. A finding you cannot reproduce is dropped, not softened, not hedged, not reworded into a "possible" finding.
- `evidence.excerpt` leads with the four labelled lines from §3.8.
- `impact` is one non-empty sentence a platform admin can act on.
- Zero findings is a valid and common result: still write the file with the populated scope.
- Validate before publishing: `audit_pr.py finish … --dry-run` renders and checks everything with no git or GitHub side effects.

### 7. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_pr.py finish --audit fleet-consistency-drift \
  --findings-file /opt/data/scratch/findings_fleet-consistency-drift.json
```

- `CLEAN` → your final response is exactly `[SILENT]`. Nothing else, no summary, no preamble.
- `OPENED` / `UPDATED` → one line naming the audit, the `new` and `resolved` counts, and the PR URL.
- A schema violation exits non-zero and publishes nothing. Fix the document and re-run `finish`. Never work around a validation error by deleting the finding that triggered it.

---

## Red Lines

- **Read-only.** Never run `gcloud container clusters update`, `node-pools update`, `kubectl apply`, `patch`, or `delete`. Remediations are text for a human, not commands to execute.
- **No external baseline.** No blueprint, standards document, CMDB, Terraform state, Config Sync repo, BigQuery, or Prometheus. If the fleet cannot tell you what normal looks like, there is no finding.
- **No delegation.** Do not create kanban cards for Cluster Agents; this audit reads the control plane itself and completes in one session.
- **No hand-written git or GitHub work.** `audit_pr.py` owns the branch, the commit, the push, the PR, and the body.
- **No unreproducible findings.** If you cannot produce the literal command that shows the value, the finding does not exist.
- **No invented field paths, label keys, or standards.** Confirm every path against real `--format=json` output and derive every expectation from the majority.
- **No findings below the floor.** Fewer than three comparable clusters, or a consensus under two-thirds, means silence — an audit that calls a two-cluster disagreement "drift" gets switched off within a week, and then it protects nothing.
