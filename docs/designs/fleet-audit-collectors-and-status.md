# Fleet Audit — Procedural Collection, Native Timing, and the Status Surface

> **STATUS — draft; not yet implemented.** Nothing described here ships yet. The behaviour that
> ships today is [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md), the design of
> record for the reporting half; this document redesigns the half it deliberately did not
> touch — how the checks get _run_ — and amends its reporting contract in exactly one place,
> named in **Builds on** below.

**Scope:** How the eight audit streams execute their checks, how long a run takes and where the
time goes, how an operator sees any of it without reading eight GitHub issues, and what it costs
to add a ninth stream.
**Builds on:** [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) — still authoritative
for the ledger, delta, promotion, and rendering contracts, with one amendment this design makes
in the same PR that needs it: its §6 "nine keys, always all nine" exit-contract sentence gains
the duration keys of §4.4. Also builds on the status-ConfigMap pattern of the self-improvement
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
i.e. the ceiling moved and the consumption did not. And the harness measures nothing:
`audit_report.py` (6,202 lines) uses `time` once, for a stderr log-line prefix. The `finish`
payload carries no timestamp, no duration, and no phase split. When a run dies at the iteration
ceiling it is reported as `timed_out` even though no clock ran out, because no component knows
how long anything took.

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
3. **Nobody can see a run.** Per-run wall-clock exists only in the on-PVC cron executions ledger
   (`executions.db`: `claimed_at`/`started_at`/`finished_at`, plus the skip vocabulary), which is
   invisible off-pod. The ledger issues carry findings, not run health. §14 of the design of
   record accepted "a dead cron is indistinguishable from a healthy fleet" — an operator today
   discovers a dead stream by noticing an absence.

## 3. Target model

Five changes, separable and independently shippable (§10):

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

What stays LLM, deliberately: triage of the 18 exception classes; GitOps declaration discovery
and manifest authoring; the three-field `recommendation` prose the validator requires non-empty
(`RECOMMENDATION_FIELDS`, `audit_report.py:246-254` — the validator enforces presence, the SOP
enforces quality); `limitations` prose for failures nobody anticipated. What stays exactly as it
is: everything in [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) except the §6
exit-payload amendment named in the header — one ledger issue per stream, generated bodies, the
delta marker, branch-name PR identity, hidden idempotency markers, promotion gating, `silent_ok`
computed in code.

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
subprocess. Nothing here re-litigates that: a scratch file, a JSON manifest, and two numbers in
an exit payload add no collector daemon and no scrape target. Operators who want dashboards can
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
healthy in-flight run never trips it; `died`-marked rows in `runs` carry the history), and
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
moving to the check table. A new short §"Collection" says: run the collector, read the
candidate list, triage what is flagged, and the four things that remain the agent's to verify
(scope reconciliation statement, limitations prose, evidence confirm reads, the findings
document). The cron prompts drop line-count geography for converted streams and instead direct
the agent to the collector first. `checks_not_applicable` mechanics are unchanged, but the
collector detects the structural cases itself (Autopilot ⇒ the four node-facing compliance
checks) and pre-fills them with the SOP's canonical reasons.

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

## 10. Work breakdown

Each phase is one PR, independently valuable, in this order:

1. **Instrumentation** — t0 phase file, `finish`/`remediate` duration keys, payload-pin
   updates, `ensure_labels` list-then-create. No behaviour change to any audit. Turns §5's
   projections into measurements.
2. **Status ConfigMap + view** — chart object plus Role/RoleBinding on the pod's KSA, the
   sidecar's internal endpoint (`credential_proxy.py`, no `command_policy.py` or operator
   change), the harness writer, `scripts/fleet_audit_status_view.py`, `make fleet-audit-view`.
   Depends on 1 for durations; ships without collectors (rows carry today's runs).
3. **Collector framework, proved against both pilot streams' full rosters.** The driver
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
   model — and its two-dump collection shape — is the same one obtainability and compliance
   already use; and `security-patch-orchestrator`, its own script (`patch_readiness.py`) since
   it reads only GKE control-plane/node-pool metadata through `gcloud container` and needs no
   kubeconfig at all — one `clusters list` call per project plus one `get-server-config` call
   per distinct location backs every one of its ten checks, where the SOP's own per-check
   command lines implied a `node-pools describe` per pool per check. Remaining:
   `fleet-wide-cost-analysis`, `fleet-consistency-drift`, `stockout-prevention`.
5. **Stream-manifest consolidation** — §4.7, plus fixing the stale "seven streams" surfaces
   (SKILL.md; `autonomous-watchdogs.md`, `governance-sops.md`, `cron-jobs.md`,
   `declarative-workflow.md` on the site) and folding the five in-flight stream PRs'
   collectors onto the framework as they land.

## 11. Files touched

| Area      | Files                                                                                                                                                                                                                          |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Harness   | `agents/platform/skills/fleet-audit/scripts/audit_report.py` (t0, duration keys, manifest cross-check, status writer, label caching), `test_audit_report.py`                                                                   |
| Collector | `agents/platform/skills/fleet-audit/scripts/collect.py` + per-stream check tables (new), tests (new)                                                                                                                           |
| SOPs      | all eight in `agents/platform/governance/` (shrink per §7), `agents/platform/cron/jobs.json` (prompts)                                                                                                                         |
| Proxy     | `agents/platform/scripts/credential_proxy.py` — one new internal route (`_handle_fleet_audit_status`) and its tests; `command_policy.py` untouched                                                                             |
| Chart     | `charts/kube-agents/templates/fleet-audit-status.yaml` (new): status ConfigMap + Role/RoleBinding on the pod's existing KSA — no new values, no operator change                                                                |
| View      | `scripts/fleet_audit_status_view.py` (new), `Makefile`, tests (new)                                                                                                                                                            |
| Docs      | this file's row in `docs/README.md`; `fleet-audit-issue-ledger.md` §6 exit-contract sentence; SKILL.md payload/field-count text; the stale stream-count pages named in §10 phase 5; generated regions via `make docs-generate` |

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

## 13. Accepted risks

- **Non-collector streams keep the attestation gap** until phase 4 lands. _Why accepted:_ the
  gap is today's steady state, and the phased order front-loads the two measured streams. _What
  it costs:_ the 2026-08-03 class stays possible on unconverted streams. _What would change it:_
  nothing; convert the streams.
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
- **No per-stream run history.** Each write replaces `last` wholesale; there is no `runs` log
  to look back through if a stream's timing regresses gradually. _Why accepted:_ nothing reads
  history today, and building it against no consumer is exactly the kind of code this design's
  premise (§2, point 2 — move deterministic work out where it earns its keep, not everywhere)
  argues against. _What it costs:_ a trend question needs the ledger issues' own dated history
  instead. _What would change it:_ a concrete reader for it — a duration-regression check in
  bench (§9), say — landing first.
- **A write that loses a race with its own successor is simply overwritten, not detected.**
  There is no read-before-write, so two calls for the same stream in quick succession (a hung
  run's `finish` racing the next day's `start`) leave whichever POST's response the sidecar
  processed last. _Why accepted:_ the window is small (runs are 10–20 minutes apart at the
  closest observed schedule gap) and the cost of losing is one stale row for one cycle, self-
  healing at the next run — far cheaper than the read-modify-write machinery avoiding it would
  cost. _What would change it:_ evidence that this actually happens in practice.
