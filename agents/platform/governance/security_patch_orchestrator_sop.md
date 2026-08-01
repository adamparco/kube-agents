# SOP: Upgrade & Patch Readiness Audit (Weekly Governance)

**Cron id:** `security-patch-orchestrator` — `20 7 * * 1` (Mondays, 07:20 UTC).

**Purpose:** Report whether every GKE cluster in the fleet runs a version its release channel still offers, and whether it is configured to _stay_ current on its own. This audit is **read-only and reports readiness**. It never upgrades anything: upgrading is a human decision, and the audit's job is to make that decision cheap, evidence-backed, and repeatable week over week.

**Data sources:** `gcloud container ...`, read-only `kubectl`, the `gke` MCP server, and the `platform_control` MCP tools (`list_cc_pods`, `get_cc_pod_diagnostics`, `list_cc_healthchecks`, `get_cc_operator_status`, `audit_log_searcher`). **Nothing else.** No BigQuery, no Prometheus, no Container Analysis or Artifact Registry vulnerability scanning, no Security Command Center, no external blueprint or CVE feed, and no delegation to Cluster Agents via kanban. **You have no vulnerability feed, so you never enumerate CVEs** — every finding here is version currency or upgrade-policy hygiene, and must be worded that way.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit security-patch-orchestrator
```

Returns `{"issue": <int|null>, "repo":"org/repo", "findings_path":"/opt/data/scratch/findings_security-patch-orchestrator.json", "pending_remediation_requests":[…]}`. Keep `findings_path`. The helper resolves the repository from the **current working directory's** git remote, so run both `start` and `finish` from the same directory — the GitOps checkout, exactly as `submit-suggestion` is invoked. `start` creates and resets no branch; there is no report branch. `issue` is this stream's open ledger issue, or `null` when it has none — either way you never create it. `pending_remediation_requests` lists finding ids a repo writer asked for with a `/remediate` comment on the ledger; write the manifest for each of those in step 4. The helper owns all git/`gh` work and renders the ledger issue body and every remediation PR body — **never hand-write a body, never run `git commit`/`gh issue create`/`gh pr create`/`gh issue comment` yourself.**

### 1. Enumerate the target fleet

1. Resolve the project scope: `gcloud config get-value project`. If `gcloud projects list --format="value(projectId)"` succeeds, include every additional project where `gcloud container clusters list` returns at least one cluster.
2. Snapshot each project once — `clusters list` returns the **full** Cluster resources, node pools included, so one call is the whole inventory:
   ```bash
   gcloud container clusters list --project=<project> --format=json
   ```
3. Record every cluster you audit in `scope.clusters` as `{name, location, project}`, plus an optional non-empty `limitations` string when some checks did not run or do not apply there. `scope.clusters` must be non-empty; if the fleet is genuinely empty, that is a hard failure of discovery, not a clean run — stop and report the error rather than emitting an empty scope.
4. **One question decides the scope list.** A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some checks did not run there, name them in that cluster's `limitations`. No → `scope.skipped`. Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator rejects a document whose two lists overlap, and any finding whose `cluster` names a `scope.skipped` entry.
5. Record every cluster you could **not** read in `scope.skipped` with a specific reason. Skip, do not flag:
   - `status` is `PROVISIONING`, `STOPPING`, or `ERROR` — the object is mid-flight or broken; version data is meaningless.
   - `enableKubernetesAlpha: true` — alpha clusters cannot be upgraded and auto-expire by design.
   - A project that errors on list (permission, API disabled). Record it as `{"cluster": "<project>/*", "reason": "…"}`.
6. Record every cluster you **could** read but could not fully check in `scope.clusters`, with the gap in its `limitations`. Autopilot (`autopilot.enabled: true`) is the standard case and is **never** skipped: Google manages those node pools, so record `{"name": "<name>", "location": "<loc>", "project": "<p>", "limitations": "Autopilot — node-pool checks 3.2/3.5/3.6/3.9 not applicable; cluster-scoped checks executed"}` and still run 3.1, 3.3, 3.4, 3.7, 3.8, 3.10. Skipping it would suppress every cluster-scoped finding you did prove there.

### 2. Establish the version baseline

Fetch the server config **once per distinct location** and cache it in memory. Do not refetch per cluster — a 40-cluster fleet in 4 regions makes 4 calls, not 40.

```bash
gcloud container get-server-config --location=<location> --project=<project> --format=json
```

Use `channels[]` (each entry: `channel`, `defaultVersion`, `validVersions[]`, and `upgradeTargetVersion` where present), plus `validMasterVersions[]`, `validNodeVersions[]`, and `validImageTypes[]`. If you are unsure whether a field exists in your gcloud version, inspect the raw `--format=json` output before relying on it — never assert a field you have not seen.

**Version comparison rule (use this everywhere, no exceptions).** Parse `MAJOR.MINOR.PATCH-gke.BUILD` into the integer 4-tuple `(MAJOR, MINOR, PATCH, BUILD)`; a version with no `-gke.N` suffix gets `BUILD = 0` (`1.30.5-gke.1355000` → `(1,30,5,1355000)`). Compare tuples element-wise as integers. **Never string-compare GKE versions** — lexically `"1.30.9" > "1.30.10"`, which is wrong — and never compare `-gke.BUILD` across different patch levels. `minor(v) = (MAJOR, MINOR)`; "N minors behind" is the difference in the second element when the first elements match, and any difference in the first element is unbounded skew.

**Universal suppression gates.** Before emitting _any_ version-drift finding (3.1, 3.2, 3.3), drop it if the cluster `status` is `RECONCILING` or the node pool `status` is `RECONCILING`/`PROVISIONING` — that is an upgrade in progress, and reporting it is noise. Policy checks (3.4–3.10) read stable configuration and still run against a `RECONCILING` cluster.

**Confirm before you emit.** The `clusters list` snapshot finds candidates; it does not justify them. For every finding, re-run a targeted, copy-pasteable command that isolates the offending field, and record _that literal command_ in `evidence.command` with its output in `evidence.excerpt`, trimmed to the 40 lines / 2000 characters the helper keeps and centred on the value that triggered the flag. **A finding you cannot reproduce is dropped, not softened.** Prefer gcloud's own `--format` projections over shell post-processing; do not assume `jq` is installed.

**Never paste a credential into an excerpt.** `clusters describe` returns `masterAuth` — client certificates, client keys, and any basic-auth password — and a service-account key can appear in a node config. None of that goes in `evidence.excerpt`. Re-run with a `--format="value(...)"` projection naming only the field you flagged, or a `-o jsonpath` that omits the secret. The harness redacts high-confidence credential shapes as a backstop, not as the primary control.

**Stable finding ids.** `spo-<check-slug>-<project>-<cluster>[-<pool>]`, taking `<check-slug>` from the backticked token in each check's heading below, lowercased, every non-alphanumeric collapsed to `-`. **Never embed a version string, timestamp, date, or count in an id** — the same problem must keep the same id across weeks or the helper's new/resolved delta is worthless. The finished id must match `^[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]$`, with no `..` segment and no `.lock` suffix: the id becomes a git branch component (`platform-agent/fix-security-patch-orchestrator-<finding-id>`), so a colon, a space, a `*`, a `..`, or a `.lock` suffix is rejected outright. The pattern also caps the id at 100 characters; when a long project, cluster, and pool triple overruns it, shorten deterministically — the same pool must produce the same id next week. All findings here are cluster-scoped, so `namespace` is always `""`.

### 3. Checks

#### 3.1 Control plane behind its release-channel baseline (`master-behind`)

- **Command:** `gcloud container clusters describe <cluster> --location=<loc> --project=<p> --format="value(currentMasterVersion,releaseChannel.channel)"`
- **Flag when:** let `C` = `releaseChannel.channel` and `B` = the cached `channels[]` entry for `C`. (a) `currentMasterVersion` is absent from `B.validVersions[]` — or, for a cluster with no channel, absent from `validMasterVersions[]`. (b) `minor(currentMasterVersion) < minor(B.defaultVersion)`. (c) same minor, but the full tuple is below `B.defaultVersion`.
- **Do NOT flag:** a master _newer_ than `defaultVersion` (channel rollout waves are staged, and RAPID's newest is routinely ahead of its default); a master that equals `defaultVersion`; a master merely below `max(validVersions)` — `defaultVersion` is the rollout target, `max()` is not.
- **Severity:** (a) **critical** — the version is no longer offered at this location, so it is outside the supported window and cannot be patched in place. (b) **major**. (c) **minor**.
- **Impact:** "Control plane `<v>` is no longer offered in the `<C>` channel at `<loc>`; the cluster is outside the supported window and receives no further patches."
- **Remediation:** `kind: gcloud`, human-executed — `gcloud container clusters upgrade <cluster> --location=<loc> --project=<p> --master --cluster-version=<B.defaultVersion>`. When the jump crosses more than one minor, use `kind: manual` instead and state that GKE upgrades one minor at a time, so the path runs through each intermediate minor.

#### 3.2 Node-pool version skew against the control plane (`pool-skew`)

- **Command:** `gcloud container node-pools describe <pool> --cluster=<cluster> --location=<loc> --project=<p> --format="value(version,status)"`
- **Flag when:** compare each `nodePools[].version` against `currentMasterVersion`. Major versions differ or the pool is **≥ 3 minors** behind; the pool is exactly **2 minors** behind; the pool is exactly **1 minor** behind; the pool is on the same minor but an older `(PATCH, BUILD)`. Separately, flag a pool whose version is **ahead of** the control plane — GKE never produces that state, so it signals a broken or hand-edited pool.
- **Do NOT flag:** Autopilot clusters (Google owns those pools; the limitation is recorded in step 1, and the cluster is still in `scope.clusters`); a pool `RECONCILING`/`PROVISIONING`, or a cluster `RECONCILING`; a pool one patch behind the control plane while the cluster is mid-rollout. GKE upgrades the control plane first and drains pools afterwards, so transient one-patch lag is normal operation.
- **Severity:** ≥ 3 minors or major mismatch → **critical** (outside GKE's documented skew policy: nodes may be no more than two minor versions behind the control plane). Exactly 2 minors → **major** (at the ceiling — the next control-plane minor upgrade is blocked until the pool moves). Exactly 1 minor → **major** if `management.autoUpgrade` is `false`, else **minor**. Patch-only drift → **minor**. Pool ahead of control plane → **major**.
- **Impact:** "Node pool `<pool>` runs `<v>`, `<n>` minor versions behind control plane `<m>` — at or beyond GKE's two-minor skew ceiling, which blocks the cluster's next control-plane upgrade."
- **Remediation:** `kind: gcloud` — `gcloud container clusters upgrade <cluster> --location=<loc> --project=<p> --node-pool=<pool> --cluster-version=<currentMasterVersion>`.

#### 3.3 Fleet-wide minor-version spread (`fleet-spread`)

- **Command:** `gcloud container clusters list --project=<p> --format="table(name,location,currentMasterVersion,releaseChannel.channel)"`
- **Flag when:** the set of distinct `minor(currentMasterVersion)` across all audited clusters spans **≥ 2 minors** (newest minor minus oldest minor ≥ 2). Emit exactly **one** finding, attached to the single most out-of-date cluster, with `object: "Cluster/<laggard>"`; name the full spread in the title and impact.
- **Do NOT flag:** a one-minor spread (normal for a fleet split across RAPID/REGULAR/STABLE); spread caused only by clusters already skipped in step 1; a second finding per laggard cluster — one fleet finding, always.
- **Severity:** **minor** — this is a fleet-consistency signal, and each individual laggard is already reported by 3.1.
- **Impact:** "The fleet spans `<oldest>`–`<newest>`, `<n>` minor versions wide; API-compatibility testing and rollout playbooks must cover every one of them."
- **Remediation:** `kind: manual` — note which clusters sit on the oldest minor and that consolidating them onto one channel narrows the spread.

#### 3.4 Cluster not enrolled in a release channel (`no-channel`)

- **Command:** `gcloud container clusters describe <cluster> --location=<loc> --project=<p> --format="value(releaseChannel.channel)"`
- **Flag when:** the field is empty, absent, or `UNSPECIFIED` — the cluster is pinned to a static version and receives **no** automatic control-plane patches.
- **Do NOT flag:** any cluster reporting `RAPID`, `REGULAR`, `STABLE`, or `EXTENDED`. Autopilot clusters are always enrolled, so this check should never fire on them.
- **Severity:** **major** — nothing is broken today, but the cluster has opted out of the mechanism that keeps it patched, so every future patch is manual work someone must remember.
- **Impact:** "`<cluster>` is on a static version with no release channel; it will not receive automatic control-plane security patches."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <cluster> --location=<loc> --project=<p> --release-channel=regular`. If the cluster is managed as a Config Connector `ContainerCluster` in the working tree, use `kind: manifest` instead (see step 4).

#### 3.5 Node-pool auto-upgrade disabled (`no-autoupgrade`)

- **Command:** `gcloud container node-pools describe <pool> --cluster=<cluster> --location=<loc> --project=<p> --format="value(management.autoUpgrade)"`
- **Flag when:** `management.autoUpgrade` is `false` or absent.
- **Do NOT flag:** Autopilot clusters; pools in a cluster already skipped in step 1.
- **Severity:** **major** — the pool will drift out of the skew window on its own and eventually block the control plane.
- **Impact:** "Node pool `<pool>` has auto-upgrade disabled; its nodes will fall behind the control plane until someone upgrades them by hand."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <pool> --cluster=<cluster> --location=<loc> --project=<p> --enable-autoupgrade`; or `kind: manifest` when the pool is a Config Connector `ContainerNodePool`.

#### 3.6 Node-pool auto-repair disabled (`no-autorepair`)

- **Command:** `gcloud container node-pools describe <pool> --cluster=<cluster> --location=<loc> --project=<p> --format="value(management.autoRepair)"`
- **Flag when:** `management.autoRepair` is `false` or absent.
- **Do NOT flag:** Autopilot clusters; pools in a cluster already skipped in step 1.
- **Severity:** **minor** — an availability and node-hygiene gap rather than a patch-currency gap.
- **Impact:** "Node pool `<pool>` has auto-repair disabled; unhealthy nodes stay in the pool until an operator notices."
- **Remediation:** `kind: gcloud` — `gcloud container node-pools update <pool> --cluster=<cluster> --location=<loc> --project=<p> --enable-autorepair`.

#### 3.7 No maintenance window configured (`no-maintenance-window`)

- **Command:** `gcloud container clusters describe <cluster> --location=<loc> --project=<p> --format="json(maintenancePolicy)"`
- **Flag when:** neither `maintenancePolicy.window.dailyMaintenanceWindow` nor `maintenancePolicy.window.recurringWindow` is present. Without a window, GKE may start automatic upgrades at any hour.
- **Do NOT flag:** a window that exists but looks narrow or awkwardly timed — that is an operator's deliberate choice, not a defect; a cluster skipped in step 1.
- **Severity:** **minor** — upgrades still happen, just at an uncontrolled time.
- **Impact:** "`<cluster>` has no maintenance window; automatic upgrades can begin during business hours."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <cluster> --location=<loc> --project=<p> --maintenance-window-start=<RFC3339> --maintenance-window-end=<RFC3339> --maintenance-window-recurrence="FREQ=WEEKLY;BYDAY=SA,SU"`.

#### 3.8 Upgrade-blocking maintenance exclusion (`blocking-exclusion`)

- **Command:** `gcloud container clusters describe <cluster> --location=<loc> --project=<p> --format="json(maintenancePolicy.window.maintenanceExclusions)"`
- **Flag when:** an exclusion is **currently in effect** (`startTime ≤ now ≤ endTime`, comparing epoch seconds via `date -u -d '<ts>' +%s` against `date -u +%s`) **and** its `maintenanceExclusionOptions.scope` blocks upgrades (`NO_UPGRADES` or `NO_MINOR_OR_NODE_UPGRADES`), **and** either it ends more than 30 days from now or the cluster already carries a critical/major finding from 3.1 or 3.2.
- **Do NOT flag:** expired exclusions; exclusions that start in the future; short freeze windows (< 30 days) on a cluster with no outstanding version finding — a holiday freeze is legitimate operational practice; `NO_MINOR_UPGRADES` scope, which still permits patch upgrades.
- **Severity:** **major** when it is holding back a cluster with a critical/major version finding, otherwise **minor**.
- **Impact:** "Maintenance exclusion `<name>` (scope `<scope>`, until `<endTime>`) is currently suppressing upgrades on `<cluster>`, which is already behind its channel baseline."
- **Remediation:** `kind: manual` — name the exclusion and its end date, and state that a human must decide whether to shorten it or accept the delay. Do not propose deleting an exclusion automatically.

#### 3.9 Deprecated or unoffered node image variant (`stale-image-type`)

- **Command:** `gcloud container node-pools describe <pool> --cluster=<cluster> --location=<loc> --project=<p> --format="value(config.imageType)"`
- **Flag when:** `config.imageType` is absent from the cached `validImageTypes[]` for that location, **or** it is exactly `COS`, `UBUNTU`, or `WINDOWS_SAC` — the pre-containerd and deprecated-servicing variants.
- **Do NOT flag:** any image type present in `validImageTypes[]` and not on the deprecated list (`COS_CONTAINERD`, `UBUNTU_CONTAINERD`, `WINDOWS_LTSC_CONTAINERD`, and whatever else that location currently offers); Autopilot clusters; casing differences — compare case-insensitively.
- **Severity:** **major** — a node image the location no longer offers cannot receive node-image patches and blocks future upgrades.
- **Impact:** "Node pool `<pool>` runs image type `<t>`, which `<loc>` no longer offers; the pool cannot take node-image patches."
- **Remediation:** `kind: gcloud` — `gcloud container clusters upgrade <cluster> --location=<loc> --project=<p> --node-pool=<pool> --image-type=COS_CONTAINERD`. Image type changes go through `clusters upgrade`, not `node-pools update`; confirm the flag with `gcloud container clusters upgrade --help` before recording it.

#### 3.10 Upgrade notifications not configured (`no-notifications`)

- **Command:** `gcloud container clusters describe <cluster> --location=<loc> --project=<p> --format="json(notificationConfig)"`
- **Flag when:** `notificationConfig.pubsub.enabled` is `false` or absent, or it is enabled but `notificationConfig.pubsub.filter.eventType[]` is set and excludes the upgrade-available event. Confirm the exact enum spelling in the raw JSON before quoting it — do not assert an enum you have not seen in output.
- **Do NOT flag:** Pub/Sub enabled with no `filter` block at all (an absent filter means _all_ event types are delivered); a pool with `management.upgradeOptions.autoUpgradeStartTime` set — that is GKE having already scheduled an upgrade, which is the system working. Attach that scheduled time to the cluster's other findings as context instead.
- **Severity:** **minor** — a visibility gap; the fleet learns about available upgrades only when this audit runs.
- **Impact:** "`<cluster>` publishes no GKE upgrade notifications, so upgrade-available signals reach no one between weekly audits."
- **Remediation:** `kind: gcloud` — `gcloud container clusters update <cluster> --location=<loc> --project=<p> --notification-config=pubsub=ENABLED,pubsub-topic=projects/<p>/topics/<topic>`; verify the flag's filter syntax with `--help` before recording it.

**Deliberately not checked.** State these in the ledger only if asked; never fabricate coverage. CVE enumeration and image vulnerability scanning are **dropped** — they need Container Analysis, Artifact Registry scanning, or an external feed, all forbidden. Calendar end-of-life ("this minor goes EOL in 45 days") is **dropped** — GKE exposes no EOL date in the API and a support-window calendar would be an external input; 3.1's "absent from `validVersions[]`" is the closest tool-derivable proxy and is what the audit actually reports. In-cluster component versions and workload image tags are out of scope: this audit covers GKE control planes and node pools.

### 4. Generate remediation artifacts

Choose `kind` by who owns the object. Grep the working tree for the cluster's Config Connector manifest (`grep -rl "name: <cluster>" --include='*.yaml' .`): if it is managed declaratively, a `gcloud` fix would be reverted on the next reconcile, so emit `kind: manifest`; otherwise emit `kind: gcloud`. Use `kind: manual` only when no single command closes the finding. Every remediation carries a `note`; **`path` is permitted only when `kind` is `manifest`** — setting it on a `gcloud` or `manual` remediation hard-fails the run. A `kind: gcloud` `note` is rendered into the ledger issue — and into the remediation PR if one ever exists — **inside a bash fence**, so it must be shell-pasteable: the command on its own line, and any caveat (a multi-minor path, a pool that will be drained and recreated, a flag whose syntax you confirmed with `--help`) as a `#` comment line above it. Prose in a `gcloud` note renders as broken shell; `kind: manual` notes are rendered as prose and should read as prose.

**Only a `manifest` finding can become a remediation PR.** Most of this audit is `gcloud` and `manual`, and those stay prose in the ledger for a human to run — a `/remediate` naming one is refused with a comment, because a PR with no diff is what this model exists to avoid. Of the manifest findings, `finish` promotes automatically only those at `critical` severity whose branch has no PR in any state, at most five per run, and names in the ledger the ones it withheld; every other manifest finding waits for a repo writer to comment `/remediate <finding-id>` or `/remediate all`. In practice nothing here auto-promotes: this audit's `critical` findings (3.1a, 3.2) are `gcloud`, and the manifest-eligible checks top out at `major`, so every manifest ships on request. Write the manifest for each id in `pending_remediation_requests` from step 0 whose finding still reproduces, or its promotion fails for want of a file. Findings whose manifest paths intersect share a single PR.

For `kind: manifest`, write the file **into the repository working tree before calling `finish`** — the helper hard-fails on a `remediation.path` that does not exist — at the repo-relative POSIX path `remediations/security-patch-orchestrator/<cluster>[-<pool>]-<check-slug>.yaml` (no leading `/` or `:`, no `..`, no glob metacharacters `*`, `?`, `[`, `]`). Generate a minimal patch of the Config Connector resource, never a full rewritten manifest: a `ContainerNodePool` carrying only `spec.management.autoUpgrade: true` (or `autoRepair: true`), or a `ContainerCluster` carrying only `spec.releaseChannel.channel: REGULAR` or the `spec.maintenancePolicy.recurringWindow` block. Keep `metadata.name`, `metadata.namespace`, and `apiVersion` identical to the live manifest so the diff is reviewable. Never generate a manifest that changes a cluster or node-pool **version** — version moves are the human's call and belong in `kind: gcloud`/`manual` notes.

### 5. Emit findings.json

Write the document to the `findings_path` from step 0 with `audit: "security-patch-orchestrator"` (it must match `--audit` exactly), the populated `scope.clusters`/`scope.skipped`, and the findings array — `[]` for a clean audit. Every finding needs a non-empty `id`, `severity`, `title`, `cluster`, `object`, `impact`, `evidence.command`, `recommendation`, and `remediation.kind`; `namespace` is `""` here. Before writing, self-check: ids unique within the file and free of versions or dates; every `evidence.command` a literal command you actually ran; `remediation.path` set for and only for `kind: "manifest"`, and present on disk; `scope.clusters` non-empty, every entry's `limitations` non-empty where present, and every `scope.skipped` entry carrying both `cluster` and `reason`, with the two lists disjoint and no finding naming a skipped cluster. A schema violation publishes nothing — `finish` exits 2 and the ledger is untouched — so validate here rather than discover it there.

**`recommendation` is required on every finding.** Three sub-fields, all non-empty strings, no exceptions. Almost nothing in this audit is promotable, and that is precisely why: a `gcloud` finding a human runs by hand needs the argument for the fix more than a mergeable diff does, and the reasoning is only cheap while the evidence is in front of you.

- `action` — what to do, imperative, one or two sentences.
- `rationale` — why this fix and not the obvious alternative; name the alternative you considered and why you rejected it.
- `risk` — what breaks on apply, and the read-only check to run first.

Worked example, for a 3.5 finding on node pool `batch-a`:

```json
"recommendation": {
  "action": "Re-enable auto-upgrade on node pool batch-a with gcloud container node-pools update batch-a --cluster=prod-usc1 --location=us-central1 --project=acme-prod --enable-autoupgrade.",
  "rationale": "Auto-upgrade is the only mechanism that keeps this pool inside GKE's two-minor skew window without a human remembering to act. The obvious alternative — leaving it off and upgrading the pool by hand each quarter — is rejected because that is the process that already failed here; the pool is behind today.",
  "risk": "GKE will drain and recreate these nodes during the maintenance window, so workloads on the pool are evicted. Confirm the window is outside business hours first with gcloud container clusters describe prod-usc1 --location=us-central1 --project=acme-prod --format=\"json(maintenancePolicy)\"."
}
```

**Size budget.** The harness caps the rendered ledger body at 60,000 characters, trims each excerpt to 40 lines / 2,000 characters and each command to 2,000, and caps the scope tables at 60 rows. On a fleet large enough to overflow, the description is truncated from the least-severe end and says so, while the title's counts remain the true totals. Trim your own excerpts to the lines that prove the finding rather than leaving that to the clipper.

### 6. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit security-patch-orchestrator \
  --findings-file /opt/data/scratch/findings_security-patch-orchestrator.json
```

One JSON line comes back with `status`, `issue_url`, `new`, `resolved`, `prs_opened`, and `prs_closed`. Exit 2 means the validator rejected the document and nothing was published — fix the document, do not retry blind. Exit 1 is fatal. Exit 0 means it published.

- `status: "CLEAN"` → the helper comments, closes the ledger issue **as completed**, and closes every open remediation PR for this stream; your final response is **exactly** `[SILENT]`. Nothing else, no preamble.
- `status: "UPDATED"` with `new: 0` **and** `resolved: 0` → the ledger was rewritten but nothing moved. Also **exactly** `[SILENT]`.
- Any other outcome → one line, then the issue URL. For example: `Upgrade & patch readiness: 3 new findings (1 critical), 2 resolved, across 11 clusters — <issue_url>`. Name any remediation PRs opened or closed in the same line.

## Red Lines

- **Never mutate.** No `gcloud container clusters upgrade`, `clusters update`, `node-pools update`, `kubectl apply/patch/delete`, or any write through the `gke` MCP server. Every remediation is a command for a human or a manifest in a remediation PR.
- **Never claim CVE coverage.** You have no vulnerability feed. "Unpatched", "vulnerable", and "CVE-XXXX" do not appear in your findings; "outside the supported window" and "behind the channel baseline" do.
- **Never invent a field.** If you have not seen a key in `--format=json` output on this fleet, do not reference it — inspect first, then assert.
- **Never emit an unreproducible finding.** No `evidence.command`, no finding.
- **Never hand-write the ledger issue body or a PR body, and never touch git/`gh` directly.** `audit_report.py` owns that surface: it creates and rewrites this stream's one ledger issue, and it opens, refreshes, and closes every remediation PR. Never open a second ledger issue, never open a remediation PR for a non-`manifest` finding, and never reopen a merged one.
- **Never paste a credential into evidence.** No `masterAuth` block, client key, token, or service-account key in an `evidence.excerpt`; re-read with a projection that omits it.
- **Never delegate to a Cluster Agent** or open kanban cards from this audit; it is a self-contained fleet read.
