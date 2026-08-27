# Fleet Audit — Procedural Collection, Native Timing, and the Status Surface

> **STATUS — implemented.** All six phases of §10's work breakdown have shipped: every stream
> now runs through a procedural collector (or names, in its own module docstring, exactly which
> of its checks it does not cover and why), native timing rides `finish`'s exit JSON and the
> status ConfigMap, `make fleet-audit-view` renders it, and §4.8's local report store is
> `finish`'s delta memory in place of the retired ledger-body read-back. Two pieces are
> deliberately not done, each with its reasoning recorded where it applies rather than here:
> `stockout-prevention`'s two beta-API/internal-log-schema checks stay prose-only (§10 phase
> 4), and §4.7's `AuditSpec` consolidation is deferred (§10 phase 5).
> [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) remains the design of record for
> the reporting half — the ledger, delta, and promotion contracts — which this document amends
> in exactly two places, named in **Builds on** below.

**Scope:** How the eight audit streams execute their checks, how long a run takes and where the
time goes, how an operator sees any of it without reading eight GitHub issues, what it costs to
read a run's findings back after it publishes, and what it costs to add a ninth stream.
**Builds on:** [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) — still authoritative
for the ledger, delta, promotion, and rendering contracts, with two amendments this design
makes, each shipping in the PR that first needs it: its §6 "nine keys, always all nine"
exit-contract sentence gains the duration keys of §4.4, and §4.8 re-points the delta's
previous-run memory from the ledger
body's hidden block to the on-pod report store (the block itself keeps being published — §4.8
says why). Also builds on the status-ConfigMap pattern of the self-improvement
loop (PR [#965](https://github.com/gke-labs/kube-agents/pull/965) — cited as the pattern's
provenance, not as a merge dependency; its details may move with its review).
**Line references** are to `c0eff3d8` and will drift; identifiers are the stable handles.

## 1. The problem, measured

Issue [#985](https://github.com/gke-labs/kube-agents/issues/985) carries the first end-to-end
timing for the audits, from the 2026-08-26 smoke run on a four-cluster fleet:

| Stream                        | Agent execution          | Output      |
| ----------------------------- | ------------------------ | ----------- |
| `compliance-audit`            | 557 s (92 % of run)      | 57 findings |
| `obtainability-audit`         | 902 s (94 % of run)      | 6 findings  |
| `security-patch-orchestrator` | ~810 s                   | 7 findings  |
| non-audit tasks, same fleet   | 92 s agent (142 s total) | —           |

The decisive comparison is inside the table. `obtainability-audit` already collects the way every
optimisation guide says to — one dump per cluster, every check read from it
(`obtainability_audit_sop.md:73`: "One JSON dump per cluster answers every check in Step 3") —
issues roughly fourteen cluster commands, finds six things, and is the _slowest_.
`compliance-audit` re-queries the fleet five times per cluster, finds fifty-seven things, and is
forty percent faster. Runtime tracks neither bytes nor findings; it tracks the check × cluster
product, at 12–20 seconds per check-cluster. Each of those is one model round trip over a
monotonically growing transcript, because every SOP presents every check as its own fenced block
and the agent executes them one shell call at a time.

The corroboration is already in the repository. `agents/platform/config.yaml:154-168` records
three runs dying at `Iteration budget exhausted (90/90)` — fixed by raising `max_turns` to 250,
i.e. the ceiling moved and the consumption did not. And, as of `c0eff3d8`, the harness measured
nothing: `audit_report.py` (6,202 lines) used `time` once, for a stderr log-line prefix; the
`finish` payload carried no timestamp, no duration, and no phase split; a run that died at the
iteration ceiling was reported as `timed_out` even though no clock ran out, because no component
knew how long anything took. §4.4/§10 phase 1 closed this: `finish` now emits `inspect_s`,
`publish_s`, and — when a manifest is present — `collect_s`.

Three further costs ride on the same architecture:

- **The attestation gap.** `validate_check_command` says it plainly: `checks_run` is
  "attestation, not verification" — "The harness runs as a subprocess of the agent; it cannot
  see the agent's tool calls" (`audit_report.py:1051-1071`). The whole `checks_run` apparatus,
  the roster-withholding error messages, and the anti-skim line-count geography in every cron
  prompt exist to make fabrication _expensive_; nothing can make it impossible while the model
  is the thing executing the commands. The 2026-08-03 five-stream false all-clear is the
  incident the apparatus answers, and [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md)
  names the residue as a residual risk the design cannot remove (its §3).
- **The defensive prose tax.** Every fragility of LLM-executed shell gets a paragraph:
  the ai-security SOP spends ~55 lines on `jq -e` dump gates, per-shell `pipefail` re-issue,
  jq exit-code taxonomy, and bare-kubectl context bleed (`ai_security_audit_sop.md:82-136`) —
  and those defences are unevenly applied. `compliance_audit_sop.md` has none of them: no dump
  gate, no pipefail, a shared `export KUBECONFIG` (`:35`), and a `$WL` that is a live command
  string re-querying the fleet per check (`:108`).
- **The extension recipe churns.** A new stream touches roughly ten artifacts across six-plus
  files (§4.7). As of 2026-08-26, five stream-adding PRs (#589, #591, #592, #594, #595) opened
  on 2026-08-08 all sit in merge conflict, each inventing its own collector script —
  simultaneously the proof that the recipe is too heavy and the precedent for the direction
  this document standardises.

## 2. Diagnosis

Three statements the rest of the design follows from:

1. **The audits are turn-bound, not API-bound.** Reducing bytes fetched (dump-once) was already
   tried by the slowest stream. The unit of cost is the model round trip, so the fix is to spend
   one round trip per _cluster_, not per _check-cluster_ — and eventually one per fleet.
2. **Detection is code that happens to be written in prose.** Classifying all 84 rostered checks
   (plus one derived) across the eight streams: 66 are fully mechanical — a deterministic
   pipeline over collected state, with closed-form severity rules and canned impact strings in
   the SOP. 18 need judgment only to apply a contextual "Do NOT flag" exception _after_ a
   mechanical candidate scan (org-email admin groups, ingress data-plane DaemonSets, free-text
   DR annotations, "latency-sensitive" qualifiers). Zero checks need judgment to detect.
3. **Nobody can see a run.** As of `c0eff3d8`, per-run wall-clock existed only in the on-PVC cron
   executions ledger (`executions.db`: `claimed_at`/`started_at`/`finished_at`, plus the skip
   vocabulary), which was invisible off-pod; the ledger issues carry findings, not run health.
   §14 of the design of record accepted "a dead cron is indistinguishable from a healthy fleet" —
   an operator discovered a dead stream by noticing an absence. §4.5/§10 phase 2 closed this: the
   status ConfigMap and `make fleet-audit-view` now answer exactly this gap.

## 3. Target model

Six changes, separable and independently shippable (§10):

1. **A per-stream procedural collector** executes every mechanical check itself — enumerating
   the fleet, fetching credentials, dumping state behind fail-closed gates, running the filters,
   and emitting (a) a labelled candidate list and (b) a machine-readable **run manifest**: per
   check per cluster, the literal command executed, its exit code, its duration, and an output
   digest. The LLM's inspection job shrinks to: run the collector, triage the candidates that
   carry a `needs_triage` exception class, author remediation and the recommendation prose, and
   write `findings.json` as today. `start`/`finish` and the whole reporting contract are
   untouched.
2. **Native timing** in the harness: `start` records t0; the collector manifest carries
   per-phase and per-check durations; `finish` computes and publishes inspect/publish durations
   in its exit JSON and in the run row it writes.
3. **A status ConfigMap**, updated on the harness's behalf — never by the agent's `kubectl`
   — at `start` and `finish`: one row per stream holding last run time, outcome, durations,
   finding counts, remediation PR count, coverage notes.
4. **`make fleet-audit-view`** renders it: one table, eight rows, with staleness flagged.
5. **One stream manifest** (§4.7): the per-stream facts collapse into a single declaration the
   roster, cron entry, collector table, and generated docs all derive from, so adding a stream
   stops touching ten artifacts.
6. **A local report store** (§4.8): `finish` keeps the document it just published —
   `latest.json` plus a short per-stream history ring on the PVC — so the chat agent answers
   "what did the audit find?" and "what changed?" from structured local files instead of a
   `gh issue view` and prose re-parsing, and `finish`'s own delta stops round-tripping its
   previous-run memory through the ledger body's hidden block.

What stays LLM, deliberately: triage of the 18 exception classes; GitOps declaration discovery
and manifest authoring; the three-field `recommendation` prose the validator requires non-empty
(`RECOMMENDATION_FIELDS`, `audit_report.py:246-254` — the validator enforces presence, the SOP
enforces quality); `limitations` prose for failures nobody anticipated. What stays exactly as it
is: everything in [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) except the two
amendments named in the header — one ledger issue per stream, generated bodies, the delta
marker as a _published_ block (§4.8 retires only its read-back), branch-name PR identity,
hidden idempotency markers, promotion gating, `silent_ok` computed in code.

## 4. Decisions

### 4.1 Collectors publish the commands they ran — the attestation contract does not change

The collector records every `kubectl`/`gcloud` invocation it makes and emits those literal
commands into the manifest; the agent copies them into `checks_run[].command` exactly as the
ai-security SOP already directs for dump-backed checks (`ai_security_audit_sop.md:50`: give the
_collection_ command, "never the `cat` of the dump"). These pass `validate_check_command`
unchanged: they name an `INSPECTION_BINARIES` member, do not match `NON_INSPECTING_COMMAND_RE`,
and do not contain the substring `audit_report.py` (`audit_report.py:1036-1102`). The validator
rejects duplicate _slugs_, not duplicate commands — one collection command legitimately backing
several checks is tested behaviour.

This strengthens the guarantee without changing its kind. Today `checks_run` is attestation
because the harness cannot observe the agent's tool calls. A collector that executes the
commands itself produces a subprocess record — command, exit code, duration, digest — and
`finish` ingests the manifest and cross-checks that every `checks_run` entry corresponds to a
command the manifest says ran. The manifest is still a file in the agent's filesystem, so a
model determined to fabricate can forge one — the claim is not impossibility. The claim is that
the 2026-08-03 shape (turn a ten-word attestation line into a published all-clear) now requires
forging a multi-field manifest with per-command digests, and the forgery is mechanically
checkable after the fact: the digests either match a re-collection or they do not. Authenticated
manifests (the collector obtaining a per-run token from the credential proxy over the manifest
digest) would close the remainder and are left as future hardening, not designed here.

_Rejected alternative:_ adding the collector's own name to `INSPECTION_BINARIES` (the allowlist
comment anticipates additions, and `test_a_command_naming_no_inspection_binary_is_rejected`
would be updated to match). Rejected because it trades away the published-falsifiability
property — a reader of the ledger can paste `kubectl get …` and re-run it; they cannot re-run
`collect_compliance.py --cluster prod` without this repository — and because a command that
names the collector says which _program_ ran, not which _cluster reads_ it performed.

_Rejected alternative:_ implementing collectors as `audit_report.py` subcommands. The validator
rejects any `checks_run` command containing that substring (`:1084-1089`), deliberately: the
harness must never appear to have inspected the fleet. Collectors are separate scripts.

### 4.2 One collector framework, per-stream check tables

The collector is one shared driver (Python, shipped like `audit_report.py`) plus a per-stream
check table: for each rostered slug, the collection command template, the filter, the closed-form
severity rule, the canned impact string, and — for the 18 — a `needs_triage` marker naming the
SOP's exception class. The driver owns fleet enumeration, per-cluster credential fetch into
per-cluster kubeconfig files, dump-once with the `jq -e` fail-closed gate, filter execution,
cross-cluster parallelism (§4.3), and manifest emission.

The check tables must key off the same roster the harness validates against.
`test_check_rosters_match_the_sops` already re-derives `AUDITS` from the SOP `####` headings;
the collector imports the roster from the same module (or a shared one), so a check cannot exist
in collector code without an SOP heading, and CI keeps SOP, roster, and collector in one triangle.

Precedent, not invention: `gcp-networking-fabric-audit` already ships `networking_audit.py`
(132 lines, one of its five checks, emitting harness-schema findings); all five open stream PRs
ship a per-stream collector of their own shape; and
`gke-cluster-autoscaler/assets/find-scale-down-blockers.sh` is a deployed skill asset that runs
many reads in one invocation and emits `=== section ===` labelled blocks. Skill scripts reach
the pod via the existing image path (`agents/platform/skills/` → `/opt/platform-template/skills`,
force-resynced by `sync_profile_skills` on every boot) and run by shebang; nothing new is needed
to ship them.

The SOPs shrink to what only prose can carry: check semantics, the "Do NOT flag" exception
classes with their reasoning, severity rationale, remediation guidance, and the red lines. The
~60 lines per SOP of pipeline-honesty scaffolding — jq exit-code taxonomy, zero-byte-dump gates,
per-shell `pipefail` re-issue, `has()` vs `//` footnotes — move into tested code and come out of
the prose. The cron prompts lose their hand-measured line-count geography once there is no
per-check prose for a skimming model to skip.

_Rejected alternative:_ a batched bash script per stream inside the SOP (one fenced block the
model runs per cluster). It captures most of the turn win and none of the other wins: the script
is retyped by the model each run (transcription drift), it cannot record a manifest the harness
trusts, timing stays unmeasured, the defensive prose stays, and the attestation gap stays. It is
the shape to fall back to for a stream whose collector has not shipped yet, not the target.

_Rejected alternative:_ delegating per-cluster collection to Cluster Agents or kanban workers.
Every stream's red lines forbid it today ("Collection is done inline, here",
`fleet_wide_cost_analysis_sop.md:260`; same in compliance, obtainability, ai-security,
security-patch), worker spawn costs 18–27 s each (measured 2026-08-07, recorded at
`agents/platform/config.yaml:12-17`), and commands published by an agent that did not issue
them would break the `checks_run` contract. Parallelism lives inside the collector process
instead.

### 4.3 Parallelism across clusters, inside the collector

The collector fans out per-cluster collection with a thread pool; wall-clock becomes
max-over-clusters instead of sum. The substrate is already safe for this: the per-cluster
kubeconfig contract exists fleet-wide precisely "so concurrent reads of different clusters do
not race on a single current-context" (`agents/platform/AGENTS.md`), the `kubectl`/`gcloud`
shims forward `KUBECONFIG` per invocation, and the credential-proxy sidecar is a threaded server
resolving the kubeconfig per request. Two rules the implementation must keep:

- **Per-command `KUBECONFIG=$KC` prefix form, never `export`.** Compliance's current
  `export KUBECONFIG` (`compliance_audit_sop.md:35`) is exactly the shared state that reports
  one cluster's contents under another cluster's name; the collector sets the env per subprocess.
- **Enumeration reconciliation is a hard step, not advice.** `coverage_gaps()` reads only the
  findings document — it has no fleet oracle. A cluster whose parallel job dies and whose output
  nobody reads would vanish from both `scope.clusters` and `scope.skipped` and the run would
  publish as a smaller, "complete" fleet. The collector therefore emits a scope skeleton —
  every enumerated cluster, as a `clusters` entry with its `checks_run`, or a `skipped` entry
  with the failure — and `finish` gains a cross-check: a manifest cluster absent from the
  document's scope is a rejection, in exactly the register of the existing scope validations.

The fail-closed dump gate is non-negotiable and gains a second justification here: the
credential proxy caps command output at 4 MiB and _preserves the command's exit code_ when it
truncates, so a big cluster's dump can arrive incomplete at exit 0. The shim does warn — a
"credential proxy output truncated" line on stderr — but a warning an agent or a
stdout-redirecting script can ignore is a signal, not a gate. `jq -e` over the dump (the
ai-security pattern) is the check that fails closed on both the zero-byte and the truncated
case; the collector applies it to every dump on every stream, ending today's unevenness where
compliance has no gate at all.

The cost stream's sampling changes sequence, not shape: today three `kubectl top` rounds with
`sleep 300` between are written inside the per-cluster block — a literal reading sleeps 600 s
_per cluster_. The collector samples all clusters back-to-back, timestamps the round, does the
non-sample collection inside the window, and sleeps only the remainder before the next round:
600 s per _fleet_, same three samples, same ≥5-minute spacing the impact lines must name, with
the four sample-dependent checks (`overrequest`, `unconsumed-pvc`, `idle-nodepool`,
`idle-namespace`) reading identical data.

_Rejected alternative:_ overlapping cluster-A triage with cluster-B collection across shell
calls (backgrounding with sentinel files). Whether the harness shell reaps background children
across tool calls is undocumented (§13 Q3); the conservative design — one blocking parallel
collection phase, then triage — captures all but max-cluster-collection-time (~30–90 s) of the
win without betting on it.

### 4.4 Timing is recorded by the harness and the collector, joined by the view

- `start` writes a phase file (t0, stream, pid) beside `findings_<audit>.json` under
  `SCRATCH_DIR`, with the same crashed-run hygiene the findings file gets (`start` deletes stale
  ones).
- The collector manifest records per-cluster and per-check durations and the collection
  totals — it is a subprocess, so this is `time.monotonic()` around `subprocess.run`, not
  estimation.
- `finish` samples its existing single `now` at entry and computes: `inspect_s` (t0 →
  finish-entry: collection + triage + authoring, the LLM-inclusive phase) and `publish_s`
  (finish-entry → exit). It emits both, plus the collector's totals when a manifest is present,
  in its exit JSON and in the status row (§4.5).
- The whole-run envelope (queue time, LLM overhead end-to-end) already exists per run in the
  cron executions ledger — which is on the PVC and stays there. The harness cannot see the cron
  layer's timestamps and the view cannot reach the pod, so the envelope remains on-pod-only
  (`hermes cron runs`, for whoever is already exec'd in) and this design does not pretend to
  join it. What the view shows is the harness's own phases; the gap is an accepted risk (§13).

Changing the `finish` payload is a pinned-contract edit, and the pins are known precisely: four
exact-dict `finish` assertions, one `remediate` assertion, and — easy to miss — `start`'s own
exact-dict payload test (`test_emits_one_json_line`), plus SKILL.md's "nine fields" sentence,
the payload comments in each SOP's publish step, and the design of record's own §6 "nine keys,
always all nine" sentence. All are updated in the same PR; every other payload assertion in the
suite is key-based and survives additive keys.

_Rejected alternative:_ exporting metrics (Prometheus, OpenTelemetry) from the harness. §14 of
the design of record rejected a metrics pipeline for run liveness because the ledger design
"deliberately holds" the boundary that its only state store is GitHub and its only runtime is a
subprocess. Nothing in this section re-litigates that: a scratch file, a JSON manifest, and two
numbers in an exit payload add no collector daemon and no scrape target. The one deliberate
exception in this design is §4.8's report store, which does carry the delta's memory on the
PVC — Q5 prices that openly rather than folding it in here. Operators who want dashboards can
build them on the ConfigMap.

### 4.5 The status ConfigMap: observability-only, sidecar-written, chart-owned

§14 of [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) accepted "a dead cron is
indistinguishable from a healthy fleet" and rejected three fixes — a heartbeat issue, state
outside GitHub, a metrics export — concluding "the right home for this is CronJob-level
alerting in the operator … Track it there, not here." This design deviates from that
conclusion openly rather than claiming continuity with it: the record lands in a
sidecar-written ConfigMap, not in operator alerting, because the fields worth recording
(findings, durations, coverage gaps) exist only inside the harness at the moment they are
true, and because the rule below forbids coupling the object to the operator at all. §14's
operator-alerting idea stays open and compatible — the controller (or anything else) can later
watch this same ConfigMap for the never-fired case its schedule knowledge covers and the
harness cannot see.

What §14 rejected on principle was state that audit _semantics_ would come to depend on: a
second source of truth for the delta, the dedup, the finding lifecycle. The status ConfigMap
is none of those, by red line (§8): **no audit decision may ever consume it** — not delta, not
dedup, not gating, not rendering, and no read-before-write on the harness side either (below).
The join keys of the audit (finding ids, branch names, hidden markers) never enter it. Every
status write is **best-effort**: any failure — the sidecar unreachable, no RBAC, the chart not
rendering the object — logs and never changes a subcommand's exit code. Nothing about the
audit changes if the ConfigMap is deleted; an operator can wipe it and lose only the last row
per stream.

**The write never touches the agent-facing `kubectl` policy, and that is the point.** The
first two drafts of this section tried to make the _agent's_ `kubectl` — the one every SOP
command runs through, gated by `command_policy.py`'s read-only allowlist — carry this one
write, first by adding a policy exception, then by binding RBAC to the identity a
no-`KUBECONFIG` call happens to authenticate as (the pod's Workload-Identity GSA). Both
required touching `command_policy.py`, the module whose own docstring calls it "the only thing
enforcing the read-only posture" — real surface to add for a telemetry sink. Neither was
necessary: **the credential-proxy sidecar already runs its own in-cluster Kubernetes identity**,
projected at `/var/run/secrets/kubernetes.io/serviceaccount` for the k8s-event-watcher it also
hosts (token, `ca.crt`, and namespace — the standard default-audience ServiceAccount mount).
The write is issued from _inside the sidecar process_, using that identity directly against
`https://kubernetes.default.svc`, through one new internal HTTP route
(`/v1/internal/fleet-audit-status`) that never appears in `/v1/exec` and is never reachable by
anything the agent's `kubectl` shim can construct. This is the same shape as the existing
`/v1/github/refresh` route: a fixed, non-agent-selectable action behind an endpoint, the
pattern `execute_internal()`'s docstring already names ("a trusted, operator-defined helper,
not agent selectable").

Consequences of routing it this way, each one a thing the ConfigMap-via-`kubectl` drafts
needed and this one does not:

- **RBAC binds the pod's real Kubernetes ServiceAccount** (`kind: ServiceAccount`), not a GSA
  email as a `User` subject — because the identity making the call is genuinely the KSA. This
  works on every install shape, with or without Workload Identity, and needs no new chart
  value: `.Values.platformAgent.security.serviceAccountName` is already referenced elsewhere in
  the chart.
- **No new environment variable, and no operator change at all.** `audit_report.py` already
  has `CREDENTIAL_PROXY_URL` (every cluster-facing command in this codebase reaches the sidecar
  through it); the sidecar already has its own namespace on disk in the same projected volume
  it reads the token from. `k8s-operator/` is untouched by this design.
- **`command_policy.py` is untouched.** The module that matters most to get right stays exactly
  as reviewed and shipped; nothing about this feature widens what an agent-issued `kubectl` or
  `gcloud` command may do.

**Writer, on the harness side:** `handle_start` posts a `started` stub (`at`, `phase`);
`handle_finish`/`handle_remediate` post the full row. The agent never calls this directly — it
is deterministic work, and this design's premise (§2, point 2) is that deterministic work
leaves inference. There is no read-before-write: each POST is a merge-patch of the stream's
`last` key in full, so a call always replaces what was there rather than building on it. This
drops the resourceVersion/retry/corrupt-guard machinery earlier drafts needed for a
read-modify-write — there is nothing to race, because nothing is read — at the cost of a
per-stream run history, which the view does not need: a died-mid-run stream is detected from
one stale `last` row (§4.6), not from a log of prior rows. A write that fails (sidecar down,
RBAC missing, chart not rendering the object) logs a warning and returns; the next run's write
is a fresh attempt, not a retry of this one.

**Schema** — one data key per stream (`compliance-audit.json`, `obtainability-audit.json`, …),
so two streams never contend for the same key:

```json
{
  "last": {
    "at": "2026-08-26T06:31:12Z",
    "phase": "finished",
    "status": "UPDATED",
    "partial": false,
    "silent_ok": false,
    "new": 3,
    "resolved": 1,
    "findings": 57,
    "critical": 2,
    "prs_opened": 1,
    "prs_closed": 0,
    "issue_url": "https://github.com/…/issues/12",
    "inspect_s": 214.0,
    "publish_s": 41.5,
    "clusters": 4,
    "skipped": 0,
    "coverage_gaps": 0,
    "note": ""
  }
}
```

`prs_opened`/`prs_closed` are counts, not the finish payload's URL lists — the URLs' durable
home is the ledger and the PR labels, and the row stays small. `note` is the first coverage
gap, truncated — the only model-influenced text in the row, already redacted where
`coverage_gaps` was assembled.

_Rejected alternative:_ one ConfigMap per stream. Eight objects multiply naming discipline,
view reads, and chart templates for no isolation gain per-stream data keys do not already
provide.

_Rejected alternative:_ per-stream run history (the `runs` array earlier drafts carried,
capped and shed like PR #965's ledger). Nothing in this design reads it — the view's every
flag derives from the single `last` row — so it was complexity with no consumer. If a future
need for history appears (a duration trend line, say), add it back deliberately, against a
reader that actually uses it.

_Rejected alternative:_ a heartbeat comment or issue on GitHub. §14 already rejected it: a
scheduled write that says "nothing happened" trains everyone to ignore the stream that one day
says something.

### 4.6 `make fleet-audit-view`

A repo script (`scripts/fleet_audit_status_view.py`) plus a thin Makefile target, mirroring
`selfimprove`'s view: reads the ConfigMap via `kubectl get configmap -o json` (no Kubernetes
client dependency), `--file`/stdin offline mode, `--json` passthrough, imports the harness's own
pure helpers for derived columns and degrades to `?` when unavailable, scrubs terminal control
characters from all model-influenced strings at one boundary.

The default render is one table, eight rows — the per-stream fleet at a glance:

```
STREAM                     ENABLED  SCHEDULE     LAST RUN (ET)      STATUS    FINDINGS  Δ        PRS  INSPECT  PUBLISH  ISSUE
compliance-audit           yes      20 6 * * *   Aug 26  6:31 am    UPDATED   57 (2 c)  +3 / −1  1    3m34s    42s      #12
obtainability-audit        yes      50 6 * * *   Aug 26  7:05 am    CLEAN ⚠   0         —        0    15m2s    38s      #14
…
```

with three flags the raw data cannot be trusted without: **stale** (now is past the next
expected fire plus slack, from the schedule — a silent stream is rendered loudly, closing the
observation hole the ledger design's §14 conceded), **died mid-run** (the latest row is a
`started` stub _and_ now is past the next expected fire — the same staleness math, so a
healthy in-flight run never trips it; the on-PVC cron executions ledger carries the history of
dead runs, not this object, whose schema keeps a single `last` row), and
**partial** (`⚠`, coverage gaps named below the table). Unknown status values render in the
warning colour, never the success one. Enabled/schedule come from the checked-in seed roster
(`agents/platform/cron/jobs.json`) with a `--roster` flag for a live runtime dump — the seed
can drift from runtime, and the view says which it read (§13 Accepted risks).

### 4.7 Extensibility: one stream manifest

Adding a stream today means editing the `AUDITS` tuple, writing the SOP, adding the cron roster
entry with hand-measured line geography, the `SOP_FILENAMES` test map, `generate_docs.py`'s
skill grouping, and three generated docs — plus four unenforced surfaces that are all
measurably stale on `main` today: the fleet-audit SKILL.md table and three docs-site pages
(`autonomous-watchdogs.md`, `governance-sops.md`, `cron-jobs.md`) still say "seven" streams
while `AUDITS` has eight, and `declarative-workflow.md`'s "cannot open a seventh stream"
aside makes a fifth. The design collapses the per-stream facts into one
declaration — the existing `AuditSpec`, grown to carry the check table of §4.2 (or a sibling
per-stream module the collector and `AUDITS` both import) — and derives the rest: the roster
test keeps re-deriving from SOP headings, the cron entry loses its geography once the SOP loses
its per-check prose, and `make docs-generate` picks up the stream tables it currently leaves to
hand editing. The five in-flight stream PRs get a migration note, not a rebase demand: their
SOPs and rosters port as-is; their bespoke collector scripts are re-homed onto the framework
when each lands.

### 4.8 A local report store, and the ledger read-back retired

Two costs share one cause. A user asking the chat agent "what did the last compliance audit
find?" or "what changed since last week?" costs a `gh issue view` plus model turns re-parsing
rendered prose back into facts the harness held in structured form seconds before it published —
the findings document survives only until the next run overwrites `findings_<audit>.json` in
scratch, so the prose on GitHub is the only copy left, and the ledger rewrites itself in place,
so run-over-run comparison has no source at all. And `finish` itself re-fetches the previous
ledger body every run to parse its own breadcrumb back out of it — the hidden
`<!-- audit-findings: … -->` block is the delta's only memory of the previous run's ids. Both
are the same missing thing: the run's structured output, kept where it was produced.

`finish` therefore keeps what it publishes. On the exit-0 path — CLEAN and findings branches
alike, never on `--dry-run`, never after a validation failure, and not from `remediate`, which
changes no findings — it writes, atomically (`os.replace` from a temp file in the same
directory), under `${HERMES_HOME:-/opt/data}/fleet-audit/reports/<audit-id>/`:

- `runs/<finished-at, UTC, filename-safe>.json` — an envelope carrying the run's outcome
  (`status`, `issue_number`, `issue_url`, `partial`, `coverage_gaps`), its three durations
  (`collect_s`/`inspect_s`/`publish_s`, each exactly as the exit payload carried them —
  `collect_s` is null on a manifest-less run, per §4.4), the delta as id **lists** rather than
  counts (`new_ids`, `resolved_ids`, `current_ids`, plus `id_scheme` — `current_ids` is the
  **rendered** set, exactly what the body's hidden block published, because the delta join is
  rendered-vs-rendered and the full set is derivable from `document`), and the full validated
  findings document under `document` — the post-validation, post-degradation document the
  ledger rendered, carried whole rather than clipped to the cells the body had room for,
  because a finding the 60k budget pushed out is still a finding the chat path should answer
  about. The body's redaction backstop is applied to every string on the way in, so the
  envelope never holds a credential shape the public issue blanked.
- `latest.json` — a byte-identical copy of the newest envelope. A copy, not a symlink: one
  fewer behaviour to ask of the 9p mount, for zero saved bytes.

`runs/` prunes at write time to the newest 14 — two weeks of a daily stream, a quarter of a
weekly one; at the ledger's own 60k-character body ceiling that bounds the store near 1 MB per
stream, ~8 MB fleet-wide, noise on the PVC. The write is best-effort under exactly the status
writer's discipline: a store that cannot be written logs a WARNING and never changes the
current run's exit code. It does, though, delete `latest.json` on its way out. A write that
failed leaves an envelope describing a run two cycles back, and nothing in the file says so —
the chat path would quote it as current, and the next run's delta would measure against a
ledger state that no longer exists. An absent store is unknowable and both readers already
handle that; a stale one is indistinguishable from a fresh one, so the cost lands on the
_next_ run's delta, which degrades as below. Pruning is the exception: it runs in its own
`try`, because a prune that fails has not damaged the memory the run just wrote, and dropping
that memory to keep the ring at 14 would trade the useful thing for the tidy one.

One asymmetry in what gets recorded is worth stating, because getting it wrong is the same
bug the whole delta exists to prevent. `current_ids` is a claim about what a _live ledger body_
renders, so it may only be empty where a body this run wrote is empty. Two of the three clean
paths qualify — the ledger was closed, or a fresh coverage ledger was opened with nothing in
it. The third does not: zero findings over incomplete coverage (§4.6) leaves the existing
ledger open and only comments on it, so the body still renders whatever the previous run put
there. Recording `[]` against that still-open issue hands the next run a _trusted_ memory of an
empty ledger, and it announces every finding the body has been carrying all along as new. That
path therefore carries the previous set forward; and where the previous set is itself
unknowable, the envelope claims no issue number at all, so it fails the next run's trust check
by design — which is the outcome an unreadable memory is supposed to have.

**Reader one: the chat path.** An agent answering a question about a stream reads
`latest.json`, where every fact it needs is a key rather than a paragraph, and answers a "what
changed" question by comparing two files in `runs/` — the ring is the only place run-over-run
documents exist anywhere, because the ledger overwrites itself. The fleet-audit SKILL.md gains
a short section naming the layout. The store is invisible off-pod by design: run _health_
off-pod is the status ConfigMap's job (§4.5), findings off-pod are the ledger's, and this
store is the on-pod answer for the agent that lives beside it.

**Reader two: `finish`'s own delta.** The previous run's ids come from `latest.json` when it
is trustworthy — its `issue_number` matches the ledger `find_existing_issue` just returned and
its `id_scheme` matches the code's — and the previous body is then not fetched at all. When no
open ledger exists, the run is genuinely first and everything present is new, exactly as
today. The remaining case — a ledger exists but the store is absent (wiped PVC), mismatched (a
ledger this pod did not write), or scheme-stale — keeps the distinction the code already drew
for an unreadable body: an _unknowable_ memory is not an _empty_ one. The docstring of the
since-deleted `fetch_issue_body` refused to conflate them ("treating it as empty would announce
every live finding as new"), and `delta_known` carries the refusal: the run publishes its
findings with **no delta claim at all** — `new: 0`, `resolved: 0`, the comment skipped, a log
line saying the
memory was lost. §4.8 re-points that existing machinery's trigger from "the body fetch failed"
to "the store is untrusted"; the semantics of a lost memory do not change, and a wiped PVC can
never put a wrong count in a public issue — it costs one cycle of delta annotation, restored
by the write that same run makes. One deliberate simplification rides along: a scheme-stale
memory used to earn its own three-way special case — new findings still announced, only
`resolved` withheld, and the CLEAN branch ignoring the scheme on purpose — which this design
collapses into the same no-claim triad. An id-scheme bump is a rare, code-authored event, and
one lost-memory semantics is worth more than that preserved corner, so the in-code
`stale_scheme` machinery is deleted alongside the read-back. With the read-back gone,
`fetch_issue_body`, `parse_finding_titles` (resolved-finding titles now ride the stored
previous document — they name resolved findings in the delta comment and in stale-PR close
comments, and with no trusted store both degrade exactly as today's unreadable-body path
does), and the ledger-body `parse_delta_block` call are deleted — not kept as a fallback. Two
memories with a precedence rule is how a divergence becomes undetectable, and the one failure
a fallback would save — a wiped PVC's single annotation-less cycle — is cheaper than the
second mechanism.

**What the hidden block becomes — published interface, not plumbing.** The
`audit-findings`/`audit-id-scheme` block stays in every ledger body, unchanged, because it was
never only `finish`'s round-trip state: it is the body's machine-readable contract.
`bench/kube_agents_bench/verifiers.py` grades every audit eval by parsing the ids out of the
published body, `bench/CUSTOM-TASKS.md` documents that as the task-author interface, and the
block is the one way a human or an external tool recovers a run's id set from the artifact
itself with no pod access. What retires is the harness treating a public issue body as its own
database — the write stays, the read-back goes. The identical block in remediation-PR bodies
keeps both its write and its read: a pull request self-describing which findings it was opened
for is what lets reconciliation work from the live PR list alone, and that list must come from
GitHub regardless, because humans merge and close PRs between runs.

_Rejected alternatives:_ the **status ConfigMap** as the store — findings documents at
60k-character scale, times eight streams, times history, is the wrong side of etcd's 1 MiB
object cap by an order of magnitude, and §4.5 pinned that surface observability-only the day
it shipped. A **SQLite ring** beside `executions.db` — the natural unit is one whole document
per run, a blob the agent reads with `jq`, not rows that any query would need to join, and a
second database on the 9p-mounted PVC buys nothing but the WAL-journal corruption class such
mounts are known to provoke. **Committing reports into the GitOps repo** — durable and
`git diff`-able, but it writes machine telemetry into the _user's_ repository, a commit per
stream per day with Config Sync/Argo churn on each, and a network read is what the store
exists to remove. **Keeping the ledger read-back as a fallback** — rejected above, in the
delta paragraph. **Removing the hidden block from bodies entirely** — breaks the bench
verifiers and every external consumer of the published interface; the block's cost is a few
invisible lines in a generated body, and its read-back was the only part worth retiring.

## 5. What the turn budget becomes

Today's obtainability run on four clusters spends ~44 iterations on check-clusters plus ~15 on
overhead. Under this design, a run is: `start` (1) + SOP read (1) + collector invocation (1,
internally parallel) + triage of flagged candidates (~1–2) + batched confirm reads (~1 per
cluster-with-findings) + `findings.json` authoring (1–2) + `finish --dry-run` (1, kept — it is
the cheap validator round) + `finish` (1) + report (1): **9–13 iterations**. The wall-clock
projection splits by stream shape: a low-finding stream lands around 2–4.5 minutes (the
collector iteration is collection-bound, 30–90 s of parallel dumps, not the 12–20 s of a chat
round trip); a finding-heavy stream adds authoring and confirm iterations that scale with
findings (compliance's 57 findings keep it well above the floor); and the cost stream keeps
its hard 600 s sampling floor whatever else improves. Against 902 s measured for
obtainability, that is still most of the time back — before counting compliance's five-fold
re-dumping or the drift stream's nineteen facets of LLM arithmetic becoming exact code. These
are projections from the measured per-iteration cost, not measurements; the instrumentation in
§4.4 is what turns them into measurements, which is why it ships first (§10).

One incidental harness cleanup rides along in phase 1 because instrumentation will make it
embarrassing: `ensure_labels` issues seven unconditional `gh label create --force` calls and
runs in `start`, `finish`, _and_ `remediate` — fourteen network round trips on a plain run,
twenty-one when anything is promoted, to assert labels that exist. List once, create the
missing, at all three call sites.

## 6. Collector output contract

The manifest is the new machine boundary between collector and harness, so it gets a shape:

```json
{
  "version": 1,
  "audit": "compliance-audit",
  "started_at": "…",
  "finished_at": "…",
  "clusters": [
    {
      "name": "prod-usc1",
      "project": "acme-prod",
      "location": "us-central1",
      "outcome": "collected",
      "commands": [
        {
          "check": "privileged-container",
          "command": "KUBECONFIG=… kubectl get deploy,sts,ds,cronjob,pod -A -o json",
          "rc": 0,
          "duration_s": 8.2,
          "output_sha256": "…"
        }
      ],
      "candidates": [
        {
          "check": "cluster-admin-binding",
          "cluster": "prod-usc1",
          "namespace": "",
          "object": "ClusterRoleBinding/legacy-admin",
          "severity": "critical",
          "excerpt": "…",
          "needs_triage": "org-email-group-downgrade"
        }
      ]
    },
    {
      "name": "dr-west",
      "outcome": "unreachable",
      "error": "get-credentials rc=1: …"
    }
  ]
}
```

Rules: every enumerated cluster appears with an `outcome`; a gate failure (zero-byte or
truncated dump) is `outcome: "gate-failed"`, never a shorter candidate list; `excerpt` is cut
from the dump under the same credential-projection rules the SOPs mandate, and the harness
redactor remains the backstop. `finish` ingests the manifest when present: it cross-checks
`scope` against manifest clusters (§4.3), copies durations into the status row, and applies
the attestation upgrade of §4.1 — **scoped to what the manifest covers**. For a cluster the
manifest marks `collected`, a `checks_run` entry whose (check, cluster) has no rc=0 manifest
command is rejected. For a cluster the manifest marks `unreachable` or `gate-failed`, the
SOP's manual path applies: the agent may hand-collect that cluster under today's attestation
semantics, and the ledger's scope table marks it manually attested — visible, not rejected.
The two regimes coexist in one run by cluster, never by check: a `collected` cluster's entries
are all manifest-checked, so a fabricated extra check cannot hide behind a real manual
fallback. Streams without a collector keep today's attestation semantics unchanged; the
cross-check activates per stream as collectors ship.

## 7. SOP and prompt changes

Per converted stream: §"Checks" keeps each `####` heading (the roster contract and the
terminology tests hang off them) but each check body shrinks to semantics — flag condition,
exception classes, severity rationale, remediation guidance — with the command/filter text
moving to the collector's own per-stream check table in `collect.py` (or its standalone
script). The instruction to run the collector first, read the candidate list, and triage what
is flagged lands as a leading bolded sentence inside the existing "Checks" section for most
streams; `obtainability-audit` is the one exception, with its own new numbered step ("§2. Run
the collector") ahead of "Checks" instead. No stream actually grew a section titled literally
"Collection" — that name did not survive contact with the eight SOPs' existing structure.
`checks_not_applicable` mechanics are unchanged, but the collector detects the structural cases
itself (Autopilot ⇒ the four node-facing compliance checks) and pre-fills them with the SOP's
canonical reasons.

All eight cron prompts now name each stream's collector script directly in place of the
line-count citation: "Run the collector (`skills/fleet-audit/scripts/collect.py
compliance-audit`) first and read its manifest before doing anything else", and the equivalent
invocation for the other seven. `test_cron_prompts_name_the_real_collector_invocation` replaces
`test_cron_prompts_cite_the_real_sop_geography` as the anti-skim safety net: every stream now
runs through a collector, so `finish --manifest-file`'s cross-check of `checks_run` against the
commands the collector actually ran (§4.1) is a stronger anti-fabrication guarantee than a
self-reported line count ever was, which is what makes retiring the citation safe rather than
merely convenient. The check-roster-matches-the-SOP invariant the old test also carried stays
covered independently, by `test_check_rosters_match_the_sops`, which scans the whole SOP file
rather than the prompt's own citation. What the new test still owes a reader: it re-derives the
collector invocation from the SOP's own "Run the collector" fenced instruction on every run and
asserts the prompt names that exact script, so an SOP's collector command and a prompt's
citation of it cannot drift apart the way a hand-measured line count once could.

## 8. Red lines

Carried forward intact from [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) §9 —
generated bodies, no direct git/gh, read-only against clusters (the collector's every kubectl
still transits the shim and its per-argv policy; a script cannot hide a mutation from a proxy
that sees each argv), never report an unread or unchecked cluster as clean, credential hygiene
with redaction as backstop. New ones:

- **No audit decision ever consumes the status ConfigMap.** No delta, dedup, gating, or
  rendering decision may read it, and a failed status write never changes a subcommand's exit
  code or behaviour. The moment it becomes load-bearing it is a second source of truth and the
  ledger design's §14 rejection applies in full.
- **The report store (§4.8) carries exactly one input: the previous run's rendered ids and
  finding titles — and nothing it carries ever gates.** Its two consumers annotate: the delta
  (counts and comment) and the resolved-finding names in stale-PR close comments. Staleness
  itself is judged from each PR body's own block, findings and promotion and dedup never read
  the store, and no other rendered text comes from it. A failed store _write_ never changes
  the current run's exit code — the cost lands on the next run's delta, which makes no claim
  and logs why. And the ledger body's hidden block keeps being written whether or not the
  store exists, because it is bench's grading interface and the body's published contract,
  not a fallback memory.
- **The status write reaches Kubernetes only through the sidecar's own internal endpoint,
  never through the agent-facing `kubectl`/`gcloud` policy.** `command_policy.py` gains no
  exception, no new verb, no resource-name awareness — this feature does not touch it, and
  widening the agent-facing gate to carry this write instead is a design change, not a
  convenience.
- **No check exists in collector code without its SOP `####` heading**, enforced the same way
  the roster already is.
- **A collector failure is a coverage gap, never a fallback to silence.** If the collector
  cannot run — entirely, or for one cluster — the SOP's manual path is still the SOP for the
  uncovered clusters (§6's manifest-scoped rule), the run goes partial where nothing collected,
  and an empty candidate list with a failed gate publishes nothing.
- **The dump gate ships with every dump.** Removing `jq -e` to save a line reintroduces the
  false all-clear shape at 4 MiB scale.
- **On a `collected` cluster, `checks_run` never names a check whose manifest command did not
  run** (enforced by `finish`); everywhere else the existing attestation rules apply verbatim.

## 9. Testing

- **Collector unit tests per check table**: golden dumps in, expected candidates out — the jq
  filters finally get the tests prose cannot have. Fault injection: zero-byte dump, truncated
  dump (4 MiB boundary), one cluster's get-credentials failing under parallelism, all
  asserting `outcome` ≠ shorter candidate lists.
- **Manifest cross-check tests in `test_audit_report.py`**: on a `collected` cluster, a
  `checks_run` entry without an rc=0 manifest command is rejected; a manifest cluster missing
  from scope is rejected; a `gate-failed` cluster with manual entries passes and renders as
  manually attested; all errors follow the no-answer-key rule (they name the file to read,
  not the roster).
- **Payload pin updates in one PR**: the four `finish` exact-dict tests, `remediate`'s one,
  `start`'s `test_emits_one_json_line`, SKILL.md's field-count sentence, each SOP's payload
  comment, and the ledger design doc's §6 exit-contract sentence.
- **Status writer tests**: the harness side (`audit_report.py`) — a well-formed row reaches
  the sidecar endpoint, write failures never alter an exit code, no proxy URL means no request
  at all. The sidecar side (`credential_proxy.py`) — the route is wired in `do_POST`, a
  malformed stream/data value is refused before any identity is read, a missing projected
  identity degrades to `503`, an apiserver refusal or timeout degrades to `502`, and none of
  this is reachable through `/v1/exec` or affected by `command_policy.py`.
- **View tests to the selfimprove view's bar**: width/ANSI invariants, scrub boundary,
  `--file` mode, degradation to `?`, stale and died-mid-run flags, unknown-status-is-warning.
- **A timing regression case in bench**: the eval harness already grades audits against the
  ledger; add the run-duration assertion once instrumentation lands so #985's table becomes a
  tracked number instead of a one-off.
- **Report-store tests (§4.8)**: the envelope is written only on the exit-0 publish path —
  never on `--dry-run`, a validation failure, or `remediate`; the write is atomic, its failure
  logs a WARNING without touching the exit code _and_ drops the `latest.json` it could not
  replace, while a failed prune keeps the memory it did write; `runs/` prunes to the newest 14
  and `latest.json` stays byte-identical to the newest ring entry; the stored `document` has
  the redaction backstop applied; a gappy-clean run that leaves the body alone carries the
  previous `current_ids` forward rather than recording an empty ledger; an envelope pin test so
  a key rename fails in CI rather than in a chat session. Delta-memory tests: the
  store is trusted only when `issue_number` and `id_scheme` both match; with no open ledger,
  first-run semantics exactly as today; with an open ledger and an absent, mismatched, or
  scheme-stale store, the run makes no delta claim (`delta_known` false — `new: 0`,
  `resolved: 0`, delta comment skipped) and logs the lost memory; the happy path issues no
  previous-body fetch at all; and the bench verifier fixtures stay untouched — the published
  body still carries the block they parse.

## 10. Work breakdown

Each phase is one PR, independently valuable, in this order:

1. **Done. Instrumentation** — t0 phase file, `finish`/`remediate` duration keys, payload-pin
   updates, `ensure_labels` list-then-create. No behaviour change to any audit. Turns §5's
   projections into measurements.
2. **Done. Status ConfigMap + view** — chart object plus Role/RoleBinding on the pod's KSA, the
   sidecar's internal endpoint (`credential_proxy.py`, no `command_policy.py` or operator
   change), the harness writer, `scripts/fleet_audit_status_view.py`, `make fleet-audit-view`.
   Depends on 1 for durations; ships without collectors (rows carry today's runs).
3. **Done. Collector framework, proved against both pilot streams' full rosters.** The driver
   (`collect.py`: fleet enumeration, per-cluster credential fetch, the fail-closed dump gate,
   cross-cluster parallelism, manifest emission) plus every check of `obtainability-audit`
   (eleven — label-selector matching for PDBs/HPAs/Services, cross-object resolution, a
   severity that forks on which of two conditions fired) and every check of
   `compliance-audit` (eleven more — a stream whose collection has no single-dump shape at
   all: a workload dump plus four more distinct `kubectl`/`gcloud` reads, RBAC subject
   classification shared between two checks, and the has()-vs-`//` distinction the SOP is
   emphatic a jq filter must get right). Generalized `collect_cluster` into a dispatcher over
   per-stream context builders so the second stream's genuinely different collection shape
   proved the seam rather than being forced into the first stream's, and the manifest
   cross-check wired into `finish` behind `--manifest-file`, opt-in per run. Both streams:
   golden dumps, fault injection (zero-byte dump, truncated dump, a `get-credentials` failure
   under parallelism, a gate failure on one of several collections failing the whole cluster
   closed), and an integration test running `collect_fleet`'s real output through `finish`'s
   own cross-check rather than asserting the two modules agree on a shape by inspection. It
   does not yet touch either stream's SOP prose or cron prompt — SOP shrink is deliberately
   its own slice, reviewable without also reviewing twenty-two checks' worth of logic in the
   same diff.
4. **SOP shrink for both pilot streams, then the remaining streams** — retire each SOP's
   command/filter prose now that a collector carries it, update the cron prompts, then the
   other six streams in turn, including the cost stream's hoisted sampling and the drift
   stream's arithmetic; stockout last, after its qualitative thresholds ("latency-sensitive",
   "substantial") are pinned in an SOP-hardening pre-PR. Done: both pilots
   (`obtainability-audit`, `compliance-audit`); `gcp-networking-fabric-audit`, converted as its
   own project-scoped collector script (`networking_audit.py`) rather than folded into
   `collect.py`, whose target model is GKE clusters reached through a kubeconfig; and
   `ai-security-audit`, folded into `collect.py` alongside the two pilots since its target
   model is the same — GKE clusters reached through a kubeconfig — even though its concrete
   collection shape is its own: a workload dump plus a Service dump, the second backing exactly
   one check (`inference-endpoint-public`) that joins it against the first, genuinely different
   from both obtainability's single dump and compliance's five distinct reads. The dispatcher
   phase 3 built already generalizes over a per-stream context builder for exactly this reason,
   so a third distinct shape needed no new script, only a new context builder;
   `security-patch-orchestrator`, its own script (`patch_readiness.py`) since
   it reads only GKE control-plane/node-pool metadata through `gcloud container` and needs no
   kubeconfig at all — one `clusters list` call per project plus one `get-server-config` call
   per distinct location backs every one of its ten checks, where the SOP's own per-check
   command lines implied a `node-pools describe` per pool per check; and
   `fleet-wide-cost-analysis`, its own script (`fleet_waste.py`) mixing cluster-named and
   `project/<id>` manifest entries the way `networking_audit.py` does, whose collector is this
   phase's "hoisted sampling": §2's three `kubectl top` samples five minutes apart are still a
   real ten-minute wall-clock cost, but the thread pool now pays it once per cluster,
   concurrently across the whole fleet, instead of serially inside one SOP-executed shell; and
   `fleet-consistency-drift`, its own script (`fleet_drift.py`) since — like
   `security-patch-orchestrator` — every facet reads only `gcloud container` metadata and needs
   no kubeconfig, and because this stream's "check" is not a per-cluster verdict at all but a
   majority vote across a cohort: the collector carries §2's cohort-building, §3's
   baseline/confidence/severity-ladder arithmetic, and the split-cluster guard, none of which
   any other converted stream needed. `stockout-prevention` closes the phase, after its own
   threshold-pinning pre-PR (`latency-sensitive` recast as an inference-workload severity
   escalation reusing `ai-security-audit`'s own §2 discriminator; `substantial`/`inUseCount <<
count` pinned to a ratio-plus-floor; "near `maxNodeCount`" pinned to `>= 90%`; a shared
   name-token "non-production" definition for the three checks that used the term undefined) —
   and even then only a **partial** conversion, deliberately: its own script (`fleet_stockout.py`)
   covers ten of twelve checks, built on the same `ComputeClass`/`Deployment`/`StatefulSet`/
   `StorageClass`/node-pool/reservation/quota reads every other collector already reads with
   confidence. `spot-scarcity-risk` and `autoscaler-out-of-resources` stay prose-only: the first
   reads a beta Spot capacity-advice API, the second parses `jsonPayload` fields out of an
   internal autoscaler-visibility log schema, and neither shape is one this repository has
   verified anywhere else — encoding a guess as tested code would make the guess look like a
   fact. Phase 4 is otherwise complete.
5. **Stream-manifest consolidation** — §4.7, plus fixing the stale "seven streams" surfaces
   (SKILL.md; `autonomous-watchdogs.md`, `governance-sops.md`, `cron-jobs.md`,
   `declarative-workflow.md` on the site) and folding the five in-flight stream PRs'
   collectors onto the framework as they land. **Stale-docs half done, §4.7 deliberately
   deferred.** All five named surfaces already say "eight" correctly (fixed before phase 4
   started); re-swept after phase 4 landed and found one more of the same class the original
   pass missed — `declarative-workflow.md`'s "a Pod runs six audit crons" — fixed to eight. The
   five in-flight stream PRs are still open on `main`, so their collectors have nothing to fold
   onto yet; that step waits for each to land. §4.7's own consolidation — growing `AuditSpec`
   to carry the §4.2 check table so a ninth stream stops touching `SOP_FILENAMES`,
   `generate_docs.py`'s skill grouping, and the docs-site counts by hand — is **not done**: it is
   a code-organization refactor across `audit_report.py` and `generate_docs.py`, both files this
   phase's work already leans on heavily and neither of which needed to change to convert all
   eight streams onto collectors. The problem it was named to fix (a stale stream count nothing
   catches) turned out to have a narrower, safer fix than restructuring `AUDITS`: a regex sweep
   over the five surfaces for a number-word next to "audit/SOP/ledger/watchdog" was tried and
   produced more false positives (historical incident counts, unrelated "six audit crons" vs.
   "eight fleet audits" both matching the same pattern) than real findings, so it was not kept as
   a permanent guard — the two real staleness incidents to date were both caught by inspection,
   not a missing mechanism, and a fragile heuristic guard would trade a rare manual sweep for a
   standing maintenance cost. Revisit §4.7 if a ninth stream is actually proposed.
6. **Done. Local report store and ledger read-back retirement** (§4.8) — the `finish`-side
   store writer (envelope, history ring, atomic replace, best-effort discipline), the delta
   re-pointed at the store — `delta_known`'s trigger moved from "the body fetch failed" to
   "the store is untrusted", per §4.8 — deletion of `fetch_issue_body`, `parse_finding_titles`,
   the `stale_scheme` machinery (§4.8's scheme-stale simplification), `FINDING_MARKER_RE`
   (whose only consumer was `parse_finding_titles`; the marker itself is still rendered), and
   the ledger-body `parse_delta_block` call (the function itself stays — remediation-PR bodies
   still use it), the SKILL.md section naming the store layout for the chat path, and the
   ledger design doc's delta-memory amendment in the same PR, per **Builds on**. The body's
   hidden block keeps being written — bench grades against it. One deliberate rollout cost: the
   first run after this ships (and after any PVC loss) finds an open ledger and no store, so
   each stream publishes once with no delta annotation — `new: 0`, `resolved: 0`, comment
   skipped, logged — and that same run's store write restores the delta from the next run on;
   keeping the old parse as a one-release migration seed was rejected as dead code in waiting
   for a transition this mild.

## 11. Files touched

| Area      | Files                                                                                                                                                                                                                                                                                                                                                                                    |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Harness   | `agents/platform/skills/fleet-audit/scripts/audit_report.py` (t0, duration keys, manifest cross-check, status writer, label caching), `test_audit_report.py`                                                                                                                                                                                                                             |
| Collector | `agents/platform/skills/fleet-audit/scripts/collect.py` (obtainability, compliance, ai-security check tables), `patch_readiness.py`, `fleet_waste.py`, `fleet_drift.py`, `fleet_stockout.py` (all new), `agents/platform/skills/gcp-networking-fabric-audit/scripts/networking_audit.py` (extended from a PSC-only helper to the stream's full roster), tests for all of the above (new) |
| SOPs      | all eight in `agents/platform/governance/` (shrink per §7), `agents/platform/cron/jobs.json` (prompts)                                                                                                                                                                                                                                                                                   |
| Proxy     | `agents/platform/scripts/credential_proxy.py` — one new internal route (`_handle_fleet_audit_status`) and its tests; `command_policy.py` untouched                                                                                                                                                                                                                                       |
| Chart     | `charts/kube-agents/templates/fleet-audit-status.yaml` (new): status ConfigMap + Role/RoleBinding on the pod's existing KSA — no new values, no operator change                                                                                                                                                                                                                          |
| Store     | (§4.8, phase 6) `audit_report.py` — store writer, delta re-pointed at the store, ledger-body read-back deleted; `test_audit_report.py`; the fleet-audit `SKILL.md` reading section; `fleet-audit-issue-ledger.md`'s delta-memory amendment; `concepts/declarative-workflow.md`'s computable-delta bullet                                                                                 |
| View      | `scripts/fleet_audit_status_view.py` (new), `Makefile`, tests (new)                                                                                                                                                                                                                                                                                                                      |
| Docs      | this file's row in `docs/README.md`; `fleet-audit-issue-ledger.md` §6 exit-contract sentence; SKILL.md payload/field-count text; the stale stream-count pages named in §10 phase 5; generated regions via `make docs-generate`                                                                                                                                                           |

## 12. Questions, resolved

**Q1. Why not fix the speed by telling the model to batch, and skip the collector?**
_Resolved:_ see §4.2's first rejected alternative. The measured failure history of this feature
is almost entirely "the model did not do what the prose said" — the fix is less prose-executed
work, not better prose.

**Q2. Does the ConfigMap contradict the design of record's "no state outside GitHub"?**
_Resolved:_ no, by construction — §4.5 and the first new red line. What §14 rejected was state
the audit's semantics depend on; this object is telemetry no audit decision reads, and deleting
it changes no audit behaviour. Where this design does deviate from §14 — a harness-written
record instead of the operator alerting §14 pointed at — §4.5 says so openly and keeps the
operator path compatible.

**Q3. Can triage overlap collection across shell calls?**
_Resolved:_ deferred — see §4.3's rejected alternative. One live test of cross-call
backgrounding can revisit it; nothing in the design precludes it later.

**Q4. Why does the harness write the status row instead of the cron layer that already has
`started_at`/`finished_at`?**
_Resolved:_ the executions ledger is Hermes-generic and on-PVC; teaching the cron layer
audit-specific fields (findings, PRs, issue URLs) would smear the feature across a patched
third-party module. The harness holds every field at exactly the moment they are true. The
`started` stub covers the died-before-finish case the harness alone would otherwise miss; the
cron envelope itself stays on-pod (§4.4, §13).

**Q5. The delta's memory moves from the ledger body to the PVC (§4.8) — is that the
state-outside-GitHub that §14 rejected?**
_Resolved:_ it is the nearest this design comes to it, and it is accepted with eyes open rather
than argued away. What keeps it inside the line: findings are recomputed from live clusters
every run, so no finding — and no false clean — can ever depend on the store; what degrades
when the store is lost is the delta family of outputs — counts, comment, the resolved names
in stale-close comments, and a clean run's resolved tally, which can then read silent — and
every one degrades to silence-about-the-delta, not to a wrong claim. §4.8 reuses the
`delta_known` semantics the code already applies to an unreadable body, so a lost memory
publishes no delta rather than announcing everything as new (the history ring's shorter reach
after a wipe is a chat-path cost, priced in §13, not an audit one). And the ids remain
recoverable from GitHub by a human, because the ledger body keeps publishing the hidden block
(§4.8): the code stops round-tripping it, the artifact stays self-describing.

## 13. Accepted risks

- **The attestation gap survives where no collector reaches.** With phase 4 complete that is
  `stockout-prevention`'s two prose-only checks and any cluster on the manual fallback after a
  gate failure. _Why accepted:_ the two checks' API and log-schema shapes are unverified (§10
  phase 4), and the manual fallback is deliberate (§6). _What it costs:_ the 2026-08-03 class
  stays possible in exactly those corners. _What would change it:_ verifying the two shapes
  against a live cluster and converting the checks.
- **The view's schedule column can lie about runtime state.** It reads the checked-in seed
  roster by default; a stream disabled at runtime shows `enabled` until `--roster` is pointed at
  a live dump. _Why accepted:_ the alternative is the view exec-ing into the pod by default,
  which makes a read-only tool depend on pod access. _What it costs:_ a mis-labelled ENABLED
  cell, with the staleness flag still firing on the missing runs. _What would change it:_ the
  runtime roster gaining an off-pod surface of its own.
- **Two writers' clocks.** `inspect_s` spans LLM work the harness cannot subdivide (collector
  time is subdivided; triage/authoring is one number). _Why accepted:_ per-iteration
  observability belongs to the Hermes runtime, not this feature. _What it costs:_ "where did
  triage time go" needs the session transcript. _What would change it:_ runtime-level turn
  timing exported per session.
- **The whole-run envelope stays on-pod.** Queue time and end-to-end wall clock live in the
  cron executions ledger on the PVC; the status row starts at the harness's t0. _Why accepted:_
  exporting it means either the view exec-ing into the pod or the cron layer growing an
  off-pod surface, both rejected above. _What it costs:_ pre-`start` overhead is invisible in
  the view. _What would change it:_ the runtime roster or executions ledger gaining an
  off-pod surface of its own.
- **The ConfigMap can be deleted or corrupted by an operator.** _Why accepted:_ it is
  telemetry, and a wipe loses only the last recorded row per stream — the next run's write
  replaces it. _What would change it:_ nothing — that property is the argument for Q2.
- **No per-stream run history in the ConfigMap.** Each write replaces `last` wholesale; there
  is no `runs` log in the status object to look back through if a stream's timing regresses
  gradually. _Why accepted:_ nothing reads history off-pod today, and building it against no
  consumer is exactly the kind of code this design's premise (§2, point 2 — move deterministic
  work out where it earns its keep, not everywhere) argues against. _What it costs:_ an
  off-pod trend question needs the ledger issues' own dated history. _What would change it:_ a
  concrete off-pod reader landing first. On-pod, §4.8's `runs/` ring is that concrete reader
  arriving: each envelope carries the run's three durations, so a trend question asked _of the
  agent_ has a structured source — the ConfigMap row itself still deliberately keeps none.
- **A write that loses a race with its own successor is simply overwritten, not detected.**
  There is no read-before-write, so two calls for the same stream in quick succession (a hung
  run's `finish` racing the next day's `start`) leave whichever POST's response the sidecar
  processed last. _Why accepted:_ the window is small (runs are 10–20 minutes apart at the
  closest observed schedule gap) and the cost of losing is one stale row for one cycle, self-
  healing at the next run — far cheaper than the read-modify-write machinery avoiding it would
  cost. _What would change it:_ evidence that this actually happens in practice.
- **The report store dies with its PVC, and the delta forgets with it (§4.8, phase 6).** A
  wiped or recreated PVC empties the store; each stream's next run publishes once with no
  delta annotation (§4.8's lost-memory semantics) and repopulates the store as it does.
  _Why accepted:_ a lost memory makes no claim rather than a wrong one, and the ledger stays
  authoritative throughout. _What it costs:_ one annotation-less cycle per stream per wipe —
  including a clean run that reports `resolved: 0` and can go silent on what would have been
  its best news — and a history ring that starts over, so "what changed?" reaches back only
  to the wipe.
  _What would change it:_ evidence of frequent PVC loss on production installs, at which
  point a one-time seed parsed from the ledger's still-published block is the cheap recovery
  to add.
- **The store shows the last published state, not the live issue (§4.8, phase 6).** A human
  who edits the ledger body after a run changes what GitHub shows and not what the chat agent
  quotes from `latest.json`, until the next run overwrites both. _Why accepted:_ ledger
  bodies are generated and rewritten in place every run — a hand edit was already a one-cycle
  artifact before this store existed, and hand-editing is not a supported workflow. _What
  would change it:_ it becoming one, which the ledger design already rejects.
