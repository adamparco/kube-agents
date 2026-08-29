# Fleet Audit — Procedural Collection, Native Timing, and the Status Surface

> **STATUS — implemented, with §4.5 being replaced.** Every stream now runs through a
> procedural collector (or names, in its own module docstring, exactly which of its checks it
> does not cover and why), native timing rides `finish`'s exit JSON, `make fleet-audit-view`
> renders the fleet, and §4.8's local report store is `finish`'s delta memory in place of the
> retired ledger-body read-back. **§4.5's status ConfigMap shipped but never worked on a real
> install and is being deleted** — the status surface moves into the report store, which §4.5
> now specifies and §10 phase 7 tracks. One further piece is deliberately not done, with its
> reasoning recorded where it applies rather than here: §4.7's `AuditSpec` consolidation is
> deferred (§10 phase 5).
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
says why). The self-improvement loop's status ConfigMap (PR
[#965](https://github.com/gke-labs/kube-agents/pull/965)) was this design's original model for
§4.5; §4.5 now records why fleet audit does not follow it.
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
   an operator discovered a dead stream by noticing an absence. §4.5/§10 phase 2 aimed at exactly
   this gap and missed it: the status ConfigMap was never created on the install (§4.5), so
   `make fleet-audit-view` printed an empty table for thirty hours while runs succeeded. The gap
   closes with the file-based surface §4.5 now specifies.

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
3. **A status surface in the report store** (§4.5): `start` writes `started.json` and `finish`
   writes `latest.json` into the stream's own report directory, carrying last run time, outcome,
   durations, finding counts, remediation PRs, and coverage gaps. No second write path, no
   cluster object, no RBAC.
4. **`make fleet-audit-view`** renders it: one table, eight rows, with staleness and death
   flagged, read off the pod through one `kubectl exec` projection.
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

**Fan-out width, and what happens past it.** Every collector runs the whole fleet at once up to
a ceiling and queues the remainder; the pool is sized
`max(1, min(len(clusters), MAX_WORKERS))`, so a four-cluster fleet gets four threads rather than
eight idle ones, and a fifty-cluster fleet runs eight at a time until it is done. The
`ThreadPoolExecutor` queue _is_ the bucketing — a worker picks up the next cluster the moment it
frees, which beats fixed batches, where the whole bucket waits on its slowest member. Nothing
hand-rolls batches.

`MAX_WORKERS` is 8 on five of the six streams. The cost stream sets 64, and the asymmetry is
deliberate: its threads spend nearly all their wall-clock asleep between the three ≥5-minute
`kubectl top` rounds, so width there costs almost no concurrent load, while a cap of 8 would
serialise a large fleet across sampling windows and stretch the run by hours. The binding
constraint on the other five is the credential proxy — one sidecar process serialising nothing
but able to be swamped — and the apiserver of each cluster being dumped, not the harness's own
CPU.

**Thread safety is a path-keying discipline, not a lock.** Two rules, and they are exhaustive
because there is no shared mutable state in the collectors beyond the filesystem:

- **A worker thread writes only to paths keyed by its own cluster.** The per-cluster state dump
  is `wra_state_{cluster}.json`; no two threads in a run can name the same file, so the
  concurrent case is not a race at all. New per-cluster artefacts follow the same naming or they
  are a bug.
- **Anything written to a shared path goes through `_atomic_write`** — a unique
  `NamedTemporaryFile` in the target's own directory, then `os.replace`. That buys two distinct
  properties: a reader never sees a torn file (the chat path reads `latest.json` at arbitrary
  times, including mid-write), and two writers resolve to last-writer-wins rather than
  interleaved bytes. It is the right primitive here precisely because the losing writer's content
  is not wanted — a whole envelope, superseded by a whole envelope.

**One run per stream at a time is enforced, by a real lock.** The store's paths —
`latest.json`, `started.json`, `findings_<audit>.json` — are keyed by _stream_, not by run, so a
second concurrent run of the same audit would overwrite the first's in-flight state. Within a run
the keying above makes that impossible; across runs it is real, because a stream has two
dispatchers (the cron scheduler and a kanban card), and a manual dispatch while a scheduled run is
in flight is an ordinary Tuesday. An advisory check-then-act guard is not enough here: the check
and the write are two syscalls, and two `start`s inside that window both pass.

**`started.json` is the lock and the liveness record at once.** One file, because the question the
lock asks — "is a run in flight, and since when" — is exactly the question the status surface
answers.

- **Acquire is `os.link` from a uniquely-named claim file onto `started.json`.** The link is
  atomic and fails `EEXIST`, so exactly one racer wins. `O_CREAT|O_EXCL` would do as well;
  `os.link` is used because the same claim file is also what the steal below swaps in.
- **A live holder is never disturbed.** A racer that loses the link reads the holder and exits
  non-zero naming its `pid` and `t0`.
- **A dead holder is stolen, atomically, exactly once.** Past the 2 h ceiling the holder is DIED
  by §4.5's rule and the stream must not stay wedged. The stealer first links its claim onto
  `.steal-<holder-nonce>` — a second atomic gate, named for the _dead claim's_ identity, so only
  one process can ever steal that particular claim — and only then `os.replace`s itself into
  `started.json`.
- **The steal token is deliberately not deleted on success.** This is the part that has to be
  written down, because it reads like litter and removing it is silently wrong: a racer that read
  the same dead claim _before_ the steal would find the token free and replace the new owner. The
  token is pruned by age instead, which is safe unconditionally — once a claim is stolen its nonce
  is gone from `started.json` for good, so no later acquire can ask for that token again.
- **A claim with no nonce of its own gets one from the file**, because the sentence above is the
  premise the whole rule rests on and an unparseable `started.json` is the case that breaks it.
  Naming every torn write `corrupt` makes `.steal-corrupt` a one-shot: the first bad write on a
  stream is stealable and every later one is refused until the token ages out — cover 4 defeating
  cover 1, with `--steal-lock` unable to clear it either, which a test caught. Naming each one
  uniquely is worse, because two processes racing the same torn write would name two tokens and
  both would steal. So the identity is the file's — inode, mtime, size — which two racers agree on
  and two successive writes do not share.
- **`finish` releases**, and the liveness states fall out of presence rather than a timestamp
  comparison (§4.5). A `finish` that crashes after publishing leaves the lock held until the
  ceiling — a stream that reads DIED for two hours and then recovers on its own, which is the
  right failure direction.

**Six covers, because a lock that wedges a stream is worse than no lock.** The failure this
design must not introduce is a claim file nothing will ever clear, and the age ceiling alone
does not rule one out — it only bounds the common case. Each cover below is a distinct state a
real pod reaches, and each makes a claim _more_ stealable, never less:

1. **Age past the ceiling**, as above: the ordinary recovery, bounded at 2 h.
2. **The container that wrote the claim is gone.** The claim records a pod instance — the boot
   id plus PID 1's start ticks, read from `/proc`, which changes on every container restart —
   and a claim whose instance does not match the reader's is dead on sight. This collapses the
   two-hour window to zero for the death that actually happens: an OOM-killed or evicted run
   whose replacement pod starts minutes later. It is a one-way signal by construction; off
   cluster, where `/proc/1/stat` is absent, it abstains rather than guessing. Measured on the
   live agent pod rather than assumed, because `/proc` under gVisor is not obviously the shape
   this reads: it is node-scoped, so the boot id is the node's and does not turn over with the
   pod, and the start ticks are counted from node boot — 8,947,590 of them at 100 Hz against a
   54.2 h node uptime puts PID 1's start 29.35 h ago, which is the pod's age. The ticks are
   therefore the half that changes on every container restart, and the boot id is doing the job
   the code claims for it: separating two nodes that happen to agree on a tick count.
3. **A future-dated claim.** `now - epoch` is negative for a claim written under a clock that
   later jumped backwards, and a negative age never reaches a positive ceiling — the stream
   would be locked until the clock caught up, which for a badly-set clock is never. An age more
   negative than the ceiling is therefore dead too.
4. **A corrupt claim.** A truncated, empty, or non-JSON `started.json` is unparseable, so no
   rule can age it out. It is dead by definition: a claim nobody can read is a claim nobody can
   be shown to hold.
5. **A `start` that fails after taking the lock releases it.** This is the one that fires most
   often, and the ceiling is the wrong answer to it: `start` claims the stream on its first line
   and holds it until `finish`, so a `start` that dies on the next line — Minty unreachable, the
   clone refused, the workspace unwritable — has claimed a run that will never happen. A minute
   of a credential broker being down would otherwise cost two hours of the stream. Every
   abnormal exit gives the claim back, and only the claim this process wrote: the release checks
   the nonce, so a run that did age out and get stolen mid-failure cannot take the new owner's
   lock down with it.
6. **`start --steal-lock`**, the operator override for the case the automatic rules miss. It goes
   through the same atomic steal rather than around it, scoped to the nonce observed when the
   command entered, so it cannot displace a run that started between that read and the link —
   an override of a specific holder, not a blanket right to the stream.

   Concurrent overrides are the one place this reads oddly, and the shape is worth stating
   because the honest version is not the tidy one. Twelve simultaneous `--steal-lock` runs on
   the live volume ended with the stream owned by exactly one of them every round, but two to
   five of the twelve **each reported success**: a late starter reads `started.json` after an
   earlier stealer has already installed its claim, adopts _that_ as its target, and takes the
   stream from it. Chain-stealing is the override behaving correctly — each run overrode the
   holder it actually observed — but it means a returned nonce is not by itself proof of
   ownership when two operators fire at once. One at a time, or read the claim back.

One more direction of failure is worth naming because it is the opposite mistake: **an
unwritable store must not stop an audit.** Taking the lock is best-effort in exactly the sense
§4.5's writer discipline means — an `OSError` from the store directory logs a warning naming the
degradation and the run proceeds unlocked. The lock exists to stop two runs corrupting each
other's telemetry; it must never be the reason a fleet goes unaudited.

**This was verified on the real filesystem, not assumed.** The store sits on the PVC, which the
agent reaches through gVisor's gofer rather than directly, and whether that gives true atomicity is
exactly the kind of thing a design should not take on faith. Do not describe the mount from
memory either: inside the container `/proc/mounts` reports `/opt/data` as **`ext4` on `/dev/sdb`**
and lists no 9p mount at all, so the "9p PVC" this document used to say was wrong on its face.
`O_CREAT|O_EXCL`, `os.link`, and `os.mkdir` were each raced by 12 threads over 60 rounds on the
live volume: exactly one winner every round for all three. `os.replace` over an existing name was
added to that set later, because the steal path ends in one and a reader-visible gap there would
admit a second holder — 2,000 replaces against four concurrent readers, and the name was never
observed missing and never once won by a reader's `os.link`. The protocol above was then raced by 16
**separate processes** over 60 rounds per property. The first version failed — two stealers both
won on 5 of 25 rounds — and that is what identified the delete-the-token bug; the corrected
protocol holds cold-race, live-holder, dead-steal, churn, and prune properties at 16 × 60. The
shipped implementation, not the prototype, was then raced again on the same twelve properties
plus the six covers above — including the ones that assert a wedge is _impossible_, which a
passing lock cannot demonstrate on its own.

**The ring stamp also gets sub-second resolution.** `%Y%m%dT%H%M%SZ` collides when two runs finish
in the same second, one silently replacing the other in `runs/`. The lock makes that near
impossible for one stream, but microseconds cost nothing and make the ring's filenames
total-ordered.

_Rejected alternative:_ `flock`/`fcntl` advisory locking. It releases on process exit, which sounds
like an advantage until the holder is a container that was OOM-killed on a node whose kubelet has
not yet reaped it, and it carries no record of _who_ holds it or _since when_ — so the status
surface would need a second file anyway. A self-expiring claim file gives liveness and mutual
exclusion from one artifact, and its recovery rule is a wall-clock ceiling any reader can evaluate.

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

- `start` writes a phase file (t0, stream, pid, nonce) as `started.json` in the stream's report
  directory, where it is simultaneously the run lock (§4.3) and the liveness stamp (§4.5).
  `finish` reads t0 from it and then releases it; "did this stream ever run" is answered by
  `latest.json`, not by a start record left lying around.
- The collector manifest records per-cluster and per-check durations and the collection
  totals — it is a subprocess, so this is `time.monotonic()` around `subprocess.run`, not
  estimation.
- `finish` samples its existing single `now` at entry and computes: `inspect_s` (t0 →
  finish-entry: collection + triage + authoring, the LLM-inclusive phase) and `publish_s`
  (finish-entry → exit). It emits both, plus the collector's totals when a manifest is present,
  in its exit JSON and in the stored envelope (§4.5).
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
build them on the store's JSON, which is stable enough to parse and versioned by
`schema_version`.

### 4.5 The status surface: two files in the report store, and no ConfigMap

§14 of [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) accepted "a dead cron is
indistinguishable from a healthy fleet" and rejected three fixes — a heartbeat issue, state
outside GitHub, a metrics export — concluding "the right home for this is CronJob-level
alerting in the operator … Track it there, not here." This design deviates from that
conclusion openly: the record lands in the harness, because the fields worth recording
(findings, durations, coverage gaps) exist only inside it at the moment they are true. §14's
operator-alerting idea stays open and compatible — the controller can still cover the
never-fired case its schedule knowledge sees and the harness cannot.

**Earlier revisions of this section put that record in a sidecar-written ConfigMap. It is
deleted, and the reason is evidence, not taste.** On the first install to run the feature the
object never existed. `charts/kube-agents/templates/fleet-audit-status.yaml` carries the
ConfigMap _and_ the Role/RoleBinding that authorise writing it, so an install updated by
patching the CR image tag — which is how this one is updated, and the release predates the
template — has neither. Every run's write returned `403` from the apiserver and `502` from the
sidecar: thirteen attempts across thirty hours, each logged only inside the sidecar container,
while `make fleet-audit-view` printed nothing and the audits themselves succeeded. A surface
built so that a silent stream is rendered loudly was itself silent for its entire existence,
and the thing that actually reported what had happened was §4.8's report store.

The lesson generalises past the deployment bug. The ConfigMap put run health on a **second
write path with its own delivery dependency** — a chart object, an RBAC pair, a sidecar route,
and a `helm upgrade` — none of which the audit needs to produce a report. A telemetry channel
that can fail while the work succeeds will eventually do exactly that, and its failure mode is
indistinguishable from the healthy fleet it was built to disprove. Status therefore moves onto
the one path that cannot fail independently of the run: the same directory, the same uid, the
same volume the report already lands on.

**Two files per stream, both in `reports/<audit-id>/`** (§4.8 owns the directory):

- **`started.json`** — written by `start`, removed by `finish`. Its presence means "a run holds
  this stream". The file mostly exists already: `write_phase_start` writes `{audit, t0, pid}` on
  every start so `finish` can compute `inspect_s`. What changes is where it lives
  (`SCRATCH_DIR/phase_<audit>.json` → `reports_dir_for(audit_id)/started.json`), that it gains a
  `nonce`, that it is created through the atomic acquire of §4.3 rather than an unconditional
  overwrite, and that `finish` releases it.
- **`latest.json`** — written by `finish`, and already the store's envelope. It gains the three
  keys the envelope lacked and the status row had: `prs_opened`, `prs_closed`, `silent_ok`.

Liveness then reads off which files are present, with one wall-clock ceiling:

| `started.json` | `latest.json` | State                                              |
| -------------- | ------------- | -------------------------------------------------- |
| absent         | absent        | never ran                                          |
| absent         | present       | completed; `latest.json` describes it              |
| age ≤ 2 h      | either        | running                                            |
| age > 2 h      | either        | **DIED** — started, never finished; lock stealable |

Presence beats timestamp comparison here, and the reason is the lock: because `finish` releases,
"a start record exists" and "a run holds the stream" are the same fact, so the status surface and
the mutual exclusion cannot disagree with each other. There is no ordering to get wrong and no
clock skew between two files to reason about.

This is **roster-independent**, which the ConfigMap's rule was not. The view's `next_fire`
parses only `M H * * *` and `M H * * D` and returns `None` for anything else, and DIED was
gated behind staleness computed from it — so on a daily stream DIED could not fire until ~25 h
after the death, and on any other cron shape it could not fire at all. A wall-clock ceiling on
an unfinished run needs no schedule knowledge and covers every shape.

**Every ConfigMap field has a file home.** Dropping the object loses no recorded fact:

| ConfigMap row field              | Where it lives now                                                                  |
| -------------------------------- | ----------------------------------------------------------------------------------- |
| `at`, `phase: "started"`         | `started.json` (`t0`); presence _is_ the phase                                      |
| `at`, `phase: "finished"`        | `latest.json.finished_at`                                                           |
| `status`, `partial`, `issue_url` | `latest.json` — already there                                                       |
| `inspect_s`, `publish_s`         | `latest.json` — already there (plus `collect_s`, which the row never carried)       |
| `new`, `resolved`                | `len(new_ids)`, `len(resolved_ids)` — already there, as the ids themselves          |
| `findings`, `critical`           | derived from `document.findings` — already there                                    |
| `clusters`, `skipped`            | `len(document.scope.clusters)`, `len(document.scope.skipped)`                       |
| `coverage_gaps` (count), `note`  | `latest.json.coverage_gaps` — the full list, not a count and a truncated first line |
| `prs_opened`, `prs_closed`       | **new keys** on the envelope, as URL lists rather than counts                       |
| `silent_ok`                      | **new key** on the envelope                                                         |

Four of those get strictly better: `coverage_gaps` becomes the whole list instead of a count
plus a truncated `note`, and the PR fields become URLs instead of counts, because a file has no
1 MiB etcd ceiling to ration bytes against.

**The split also removes a live defect.** `_status_post` sent `{"last": row}` into a merge-patch
of one ConfigMap key, so the whole row was replaced on every call — and `handle_remediate`'s row
carries no findings, no delta, no `issue_url`. A `/remediate` after a finished audit therefore
blanked that audit's row until the next scheduled run. One slot with two writers is the bug;
per-run files remove the class rather than patching the instance.

**Writer discipline is unchanged and still best-effort.** No audit decision reads either file
for its status content (§8), and a write that fails logs and never changes an exit code. What
changes is that a failed status write now implies a failed report write — they are the same
write to the same directory — so the failure is visible in the place an operator is already
looking, instead of in a sidecar log nobody read for thirty hours.

_What this gives up:_ off-pod reads. `kubectl get configmap` needed no pod name; the files need
`kubectl exec` (§4.6). The honest counter is that the ConfigMap's off-pod readability bought
nothing during the thirty hours it was refusing writes, and a pod too sick to exec into is a
louder signal than a stale table.

_Rejected alternative:_ **fix the ConfigMap by running `helm upgrade`.** It is one command and
it makes the object appear. It also keeps a chart template, an RBAC pair, a sidecar route, and
~827 lines that no other part of the feature needs, and leaves the independent-failure property
intact — the next install updated by image tag has the same silent hole. The deployment gap was
the symptom; the second write path was the cause.

_Rejected alternative:_ **the session-KV database** (`session_kv_server.py`, SQLite on the
`system-metadata` PVC behind a local HTTP API). It is genuinely well-placed — same uid as the
writer, key already in the pod's env, and `intercepted_events` is a working precedent for a
durable run record. It loses on cost: a new table, two new routes, a TTL exemption in
`cleanup_old_records`, and their tests and docs, all in a service this feature otherwise does
not touch, to store what a file already stores. It also adds a second SQLite writer to a
sandboxed PVC mount with a WAL-corruption history, and needs a `GET` route only because `sqlite3` is
absent from the image — opacity the design would be introducing and then paying to undo.

_Rejected alternative:_ the Hermes-owned databases (`cron/executions.db`, `cron/notepad.db`,
`kanban.db`). The executions ledger already records `claimed`/`running`/`completed`/`failed`/
`unknown` per job with a first-class CLI, and its `unknown` status is precisely the DIED case.
Two things rule it out: the audit-specific fields (findings, PRs, coverage gaps) do not belong
in a Hermes-generic table, and **kanban-dispatched runs never reach it** — the run that
finished 2026-08-27 21:40:45Z is absent from `executions.db` entirely, because the dispatcher
was a kanban card rather than the scheduler. A liveness surface blind to a whole dispatch path
is not a liveness surface.

_Rejected alternative:_ a heartbeat comment or issue on GitHub. §14 already rejected it: a
scheduled write that says "nothing happened" trains everyone to ignore the stream that one day
says something.

### 4.6 `make fleet-audit-view`

A repo script (`scripts/fleet_audit_status_view.py`) plus a thin Makefile target, mirroring
`selfimprove`'s view: `--file`/stdin offline mode, `--json` passthrough, imports the harness's
own pure helpers for derived columns and degrades to `?` when unavailable, scrubs terminal
control characters from all model-influenced strings at one boundary.

**The read is a projection, not a directory walk.** The store lives on the pod's PVC, so the
view runs one `kubectl exec` against the agent pod, piping in
`agents/platform/skills/fleet-audit/scripts/report_status.py` (`kubectl exec -i … -- python3 -`)
and reading one JSON document back. Streaming the script in rather than calling an installed
path keeps the view working against any image, including one built before this change. The
projection returns only what the table needs — never whole envelopes, which run to megabytes —
plus two keys that exist to make failure legible: `root_exists`, false when the store directory
itself is missing, and a per-stream `error` for a file that is present but unreadable or
unparseable.

Those two keys close the hole that let the ConfigMap fail silently for thirty hours. The
existing `flags_for` computes staleness from the last run's timestamp, so a stream with **no**
history takes `at is None` → `expected is None` → `stale is False`, and DIED is nested inside
`stale` — no flag can fire. Every failure therefore converged on the same calm table of
`never ran` with an empty FLAGS column and exit 0: store missing, pod unreachable, and a genuinely
never-scheduled stream were indistinguishable. Under the file model the view distinguishes
**never ran** (roster-enabled, neither file present, and the store readable — flag `NEVER`, not
blank), **unreadable** (`root_exists` false or a per-stream `error` — flag `NO STORE`), and
**stale/died** (the §4.5 rule). Exit is non-zero when the store cannot be read, because "I could
not look" must not render as "nothing is wrong".

**What changes at the command surface**, since the view no longer reads a namespaced object:

- **`--pod` and `--container`, with discovery as the default.** The target is found by label
  rather than named, so the ordinary invocation stays argument-free; `--pod` overrides it when
  more than one agent pod is running, and `--container` defaults to the agent container rather
  than the sidecar. `--namespace` keeps its current meaning and its current default.
- **`--file`/stdin becomes more useful, not less.** It now takes the projection's JSON, so the
  offline mode is a real reproduction of the online one — `--json` on a live run produces exactly
  what `--file` consumes, which is what makes the view testable without a cluster.
- **The Makefile target's help text changes.** It currently reads "Render the fleet-audit status
  ConfigMap", which `make help` prints; leaving it would document an object that no longer
  exists. The recipe itself keeps passing `$(ARGS)` through unchanged.
- **The failure message names the reason.** "No agent pod found in namespace X", "pod found but
  exec denied", and "store directory absent on the pod" are three different problems and read as
  three different lines; the old path could only say nothing at all.
- **The context is found rather than demanded.** The commonest cause of "no agent pod" is not a
  broken install but a current context pointing at one of the clusters the audit _manages_,
  which any parallel session running `kubectl config use-context` leaves behind. So when the
  current context has none, the view probes the rest of the kubeconfig in parallel — time-boxed,
  because a context naming a deleted cluster blocks until its own timeout — and reads the one
  that does. Silently: the header's `context` field already names what was read, and a note on
  stderr saying the same thing is one more line between the operator and the table. Two or more
  is genuinely ambiguous and gets the error and the list. An explicit `--context` is never
  overridden: someone who named a cluster and got nothing wants to know that, not to be quietly
  redirected. `--context` also exists so that pinning the hub does not mean repointing a
  kubeconfig other sessions are using.

The default render is a header and one table, eight rows — the per-stream fleet at a glance:

```
fleet-audit  ·  8 streams  ·  2 need attention  ·  last run 6m ago

  store     /opt/data/fleet-audit/reports
  source    kubeagents-system/platform-agent-gateway-5959464c4f-t4lvq [platform-agent]
  context   gke_adamparco-kage_us-east4_kube-agents-host
  roster    agents/platform/cron/jobs.json
  findings  68 across 8 run streams · 18 critical
  health    ██████████████░░░░ 6 of 8 streams clean

STREAMS
┌──────────────────────┬─────┬────────────┬─────────────────┬───────────┬───────────┬───────────┬───────┐
│ STREAM               │ ON  │ SCHEDULE   │ LAST RUN        │       AGE │ STATUS    │  FINDINGS │ ISSUE │
├──────────────────────┼─────┼────────────┼─────────────────┼───────────┼───────────┼───────────┼───────┤
│ compliance-audit     │ yes │ 20 6 * * * │ Aug 29 7:24 am  │ 2h47m ago │ UPDATED   │   6 (5 c) │   #57 │
│ stockout-prevention  │ yes │ 20 9 * * * │ Aug 29 8:20 am  │ 1h51m ago │ UPDATED ⚠ │ 16 (12 c) │  #110 │
└──────────────────────┴─────┴────────────┴─────────────────┴───────────┴───────────┴───────────┴───────┘
```

The table, the palette, OSC 8 links and the width fitting are imported from
`scripts/selfimprove_ledger_view.py` rather than reimplemented; two terminal tables in one
repository that disagree about how to measure a coloured cell is two bugs. `--color`, `--ascii`,
`--utc` and `--width` carry the same meanings there as here, joined by `--sort`,
`--stream`/`--flagged` to narrow the rows, `--gaps` to open the coverage-gap detail, and
`--watch N` to redraw in place. A filter that hides rows says how many it hid, a width that
drops columns names them, and the default view counts the coverage gaps it is not printing —
something the surface quietly omits is the silent-clean failure this section exists to close,
in a new place.

`--gaps` is where that rule and readability had to be traded against each other. The live
install's gaps are four-sentence paragraphs — nine of them, most differing only in the cluster
name — and printing all nine under the table pushes the table itself off the terminal, which is
the one thing the view exists to show. So the default keeps only the count (`9 coverage gaps in
2 streams; --gaps for the text`) and the flag opens a second table: stream, the scope the gap
names, and the text, wrapped rather than clipped to the terminal's spare width. The scope is
split off only when the prefix reads like one — short and unspaced — so a gap whose prose
happens to contain a colon stays whole.

with the flags the raw data cannot be trusted without: **stale** (now is past the next expected
fire plus slack, from the schedule — a silent stream is rendered loudly, closing the observation
hole the ledger design's §14 conceded), **died mid-run** (`started.json` is still present and its
`t0` is more than two hours old, so a healthy in-flight run never trips it), **never**
(roster-enabled, store readable, neither file present), **no store** (the read itself
failed), and **partial** (`⚠`, coverage gaps named below the table). Unknown status values render
in the warning colour, never the success one.

DIED is the flag that changes character here. It previously required the staleness computation
to have produced an expectation, and `next_fire` handles only `M H * * *` and `M H * * D` — so on
a daily stream the flag arrived roughly a day after the death, and on any other cron shape it
never arrived. Reading it from one file's presence and a wall-clock ceiling makes it fire within
two hours on every schedule shape, including streams dispatched from a kanban card that have no
schedule at all. Enabled/schedule still come from the checked-in seed roster
(`agents/platform/cron/jobs.json`) with a `--roster` flag for a live runtime dump — the seed can
drift from runtime, and the view says which it read (§13 Accepted risks) — but only STALE and
NEVER depend on the roster now, so roster drift can no longer suppress the death of a running
stream.

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
directory), under `/opt/data/fleet-audit/reports/<audit-id>/` — a fixed root rather than
`$HERMES_HOME`, because `finish` runs under a cron or kanban worker, which the dispatcher spawns
with `HERMES_HOME` pointed at the profile directory, while the chat path that reads the store back
runs in the gateway process, where it is `/opt/data`. Rooting the store at `$HERMES_HOME` writes it
to one path and reads it from the other, and the run-to-run delta agrees with itself either way, so
the only symptom is a chat path that never finds a report:

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
  fewer behaviour to ask of the sandboxed mount, for zero saved bytes.

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
a short section naming the layout — which §4.9 turns into its own reader skill and a bounded
query script, because a description advertising only publishing is not discoverable from "what
did last night's audit find", and reading a whole envelope to answer a one-number question is not
affordable. Findings off-pod remain the ledger's job; the store is the on-pod answer for the agent
that lives beside it, and — since §4.5 — the on-pod answer for run health too, which
`make fleet-audit-view` projects off-pod through a single `kubectl exec` (§4.6) rather than
through a second write path.

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
object cap by an order of magnitude. That reasoning held; what changed since is the direction
of travel, because §4.5 has now moved the small status row _into_ this store rather than the
documents into the ConfigMap. A **SQLite ring** beside `executions.db` — the natural unit is one whole document
per run, a blob the agent reads with `jq`, not rows that any query would need to join, and a
second database on the PVC buys nothing but the WAL-journal corruption class such
mounts are known to provoke. **Committing reports into the GitOps repo** — durable and
`git diff`-able, but it writes machine telemetry into the _user's_ repository, a commit per
stream per day with Config Sync/Argo churn on each, and a network read is what the store
exists to remove. **Keeping the ledger read-back as a fallback** — rejected above, in the
delta paragraph. **Removing the hidden block from bodies entirely** — breaks the bench
verifiers and every external consumer of the published interface; the block's cost is a few
invisible lines in a generated body, and its read-back was the only part worth retiring.

### 4.9 Answering a chat user's question about a run

§4.8 named the chat path as the store's first reader and left it at "the fleet-audit SKILL.md
gains a short section naming the layout". That section exists and is accurate, and it is still
not enough for a user who types "what did last night's compliance audit find?". Three things sit
between the question and the files, and each needs a different fix.

**The Planning Agent cannot read the store, and must not be taught to.** The `default` profile
that receives chat ingress holds no file tools at all — its tools are `list_agents`,
`kanban_create`, the `kanban_list`/`show`/`comment`/`unblock` board reads, and the `memory_*`
family. Every substantive question is delegated with `kanban_create`, and its own instructions
already say to default to `platform` for fleet and knowledge questions. So the routing works
today and this design changes nothing about it. What is worth writing down is the consequence:
**the reader is the platform specialist spawned by the kanban dispatcher**, not the conversational
front door, and the store's fixed `/opt/data` root (rather than `$HERMES_HOME`) is what makes the
same path resolve for both the cron worker that wrote it and the specialist that reads it.

**The skill that documents the store advertises only writing.** `fleet-audit`'s frontmatter
description is "Publish the findings of an autonomous fleet audit as one continuously-rewritten
GitHub issue per audit stream, and propose fixes as narrow remediation pull requests." Skill
selection runs off that sentence, and it contains no signal that the skill also answers questions
about past runs — the reading section is at line 85 of 798. An agent handed "what did the audit
find" has to already know to open a skill about publishing. **The reader therefore splits into its
own skill, `fleet-audit-reports`**, whose description names the questions it answers: what a
stream last found, what changed between runs, which clusters were skipped, when each stream last
ran. The writer skill keeps the lifecycle and loses the reading section to a pointer. The split is
also the cheaper context: a read question stops pulling 798 lines of publish/remediate procedure
into the window to reach one paragraph.

**Reading the files directly does not scale to the context window.** `latest.json` embeds the
whole findings document — deliberately un-clipped, so it can exceed the 60k-character budget the
issue body is held to — and there are eight streams with a fourteen-run ring behind each. An agent
that answers "how many criticals are open on compliance?" by reading `latest.json` spends tens of
thousands of tokens to produce a number. So the reader skill's real content is a query script,
`report_query.py`, with subcommands that each return a small JSON document:

| Subcommand                      | Answers                                                                    |
| ------------------------------- | -------------------------------------------------------------------------- |
| `streams`                       | one line per stream: last run, status, counts, liveness (§4.5)             |
| `show <stream>`                 | one run's envelope **without** `document` — status, delta, durations, gaps |
| `findings <stream>`             | finding titles/severities/clusters, filterable, never full bodies          |
| `finding <stream> <id>`         | one finding in full — the only path that returns prose                     |
| `diff <stream> [--from] [--to]` | ids and titles added/resolved between two runs in the ring                 |
| `runs <stream>`                 | what the ring holds, so a diff can name real stamps                        |

The discipline is one rule: **every subcommand's output is bounded and the full document is
opt-in.** `show` omits `document` precisely because including it would make the cheap call the
expensive one, and `finding` exists so the expensive call is still available at the granularity
someone actually asked for.

This is the same projection idea §4.6 uses for the view, and the two deliberately share
`report_status.py`'s reading helpers rather than growing a second parser of the same files. The
difference is the consumer: §4.6 renders a fixed operator table off-pod, while `report_query.py`
runs on-pod for an agent that has already been asked something specific.

_Rejected alternative:_ giving the Planning Agent file tools so it can answer directly and skip a
delegation hop. It would turn the one profile with no infrastructure access into one with
filesystem access, for latency on a question that is already asynchronous by design, and every
other specialist capability has resisted exactly this. The delegation hop is the architecture, not
an obstacle to route around.

_Rejected alternative:_ a single `report_query.py summary` that returns everything and lets the
model pick. That is the behaviour the subcommands exist to prevent — it reintroduces the
whole-document read with extra steps, and it is the shape that makes a cheap question expensive.

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

- **No audit decision ever consumes the status files.** `started.json` is read by exactly two
  things: `finish`, for `inspect_s`, and the run lock (§4.3). No delta, dedup,
  gating, or rendering decision may read either file _for its status content_, and a failed
  status write never changes a subcommand's exit code. `latest.json`'s separate role as the
  delta's memory is bounded by the next red line, which is narrower than this one and governs
  it. The moment run health becomes load-bearing it is a second source of truth and the ledger
  design's §14 rejection applies in full.
- **The report store (§4.8) carries exactly one input: the previous run's rendered ids and
  finding titles — and nothing it carries ever gates.** Its two consumers annotate: the delta
  (counts and comment) and the resolved-finding names in stale-PR close comments. Staleness
  itself is judged from each PR body's own block, findings and promotion and dedup never read
  the store, and no other rendered text comes from it. A failed store _write_ never changes
  the current run's exit code — the cost lands on the next run's delta, which makes no claim
  and logs why. And the ledger body's hidden block keeps being written whether or not the
  store exists, because it is bench's grading interface and the body's published contract,
  not a fallback memory.
- **The status write never reaches Kubernetes at all.** It is a file write on the pod's own
  PVC. `command_policy.py` gains no exception, no new verb, and no resource-name awareness; no
  RBAC is granted to any identity for this feature; and re-introducing a cluster object as the
  status surface is a design change, not a convenience.
- **A worker thread writes only to cluster-keyed paths, and every shared path goes through
  `_atomic_write`** (§4.3). A collector that writes an un-keyed path from inside the pool, or
  that writes a shared path with a plain `open()`, is a bug regardless of whether the race has
  been observed.
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
- **Status file tests** (`test_audit_report.py`): `start` writes `started.json` into the
  stream's report directory and `finish` removes it; a `finish` with no `started.json` still
  writes `latest.json` and reports `inspect_s` as unknown rather than failing; an unwritable
  report directory logs and exits 0.
- **Liveness truth table**: each of §4.5's four states asserted directly against fixture
  directories — neither file; `latest.json` with no `started.json`; a `started.json` inside the
  ceiling; one past it — plus the case that motivated the redesign, a stream whose cron
  expression `next_fire` cannot parse, which must still report DIED.
- **Concurrency tests**: a collector run against N fake clusters asserts the pool is capped at
  `MAX_WORKERS`, that a 3-cluster fleet spawns 3 threads rather than 8, and that every
  per-cluster artefact path is distinct; a direct test drives concurrent `_atomic_write` calls
  on one path and asserts every read observes a complete document and the final content is one
  writer's whole output, never a splice; a ring test writes two envelopes in the same second and
  asserts two files.
- **Lock tests, with real processes.** Threads share a file-descriptor table and would pass a
  protocol that separate processes break, so these fork: N processes released from a barrier
  against one directory assert exactly one acquires; a fresh holder is never stolen; N processes
  racing a planted dead holder assert exactly one steals it _and_ that `started.json` ends up
  holding the winner's nonce; acquire/release churn never double-admits. One test pins the bug
  the torture run found — a steal token deleted on success lets a late racer replace the new
  owner — by asserting the token survives its own steal, and a second asserts it is pruned once
  older than the ceiling. At the command surface, `start` refuses while a live claim is held and
  says whose pid and start time hold it, and proceeds once that claim ages past the ceiling.
  Each of §4.3's six covers gets its own test, because a cover is an assertion that a wedge is
  _impossible_ and a lock that merely works cannot demonstrate one: a claim from a departed
  container is stolen, a future-dated claim is stolen rather than waited out, a corrupt claim
  does not refuse forever, a `start` that fails after taking the lock leaves no claim behind and
  a second attempt is not refused — while one whose claim was stolen while it failed leaves the
  new owner's alone — N concurrent `--steal-lock` runs leave the stream owned by exactly one of
  them, and an unwritable store returns an unlocked run rather than raising. These run
  against `tmp_path` in CI; the protocol was separately verified on the live volume at 16
  processes × 60 rounds, and the shipped implementation re-raced there at 12 processes × 20
  rounds across all nine properties, including the two covers a macOS CI host cannot reach —
  the departed-container steal and the atomicity of the rename the steal path ends in (§4.3).
- **Query-script tests** (§4.9): each subcommand's output is JSON and bounded — `show` must not
  contain `document`, `findings` must not contain finding bodies — and a store with a missing,
  empty, or corrupt `latest.json` produces a stated error rather than an empty success. One test
  asserts the skill's frontmatter description mentions reading past runs, since that sentence is
  the entire discovery mechanism.
- **View tests to the selfimprove view's bar**: width/ANSI invariants, scrub boundary,
  `--file` mode, degradation to `?`, stale/died/never/no-store flags,
  unknown-status-is-warning, and — the regression that hid the ConfigMap failure — a
  roster-enabled stream with no history renders `NEVER`, while an unreadable store renders
  `NO STORE` and exits non-zero. Neither may render as a blank FLAGS column.
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
2. **Shipped, and superseded by phase 7. Status ConfigMap + view** — chart object plus
   Role/RoleBinding on the pod's KSA, the sidecar's internal endpoint (`credential_proxy.py`),
   the harness writer, `scripts/fleet_audit_status_view.py`, `make fleet-audit-view`. The view
   and the Makefile target survive phase 7; everything that writes to Kubernetes does not.
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
   and its own script (`fleet_stockout.py`) now covers all twelve. Ten are built on the same
   `ComputeClass`/`Deployment`/`StatefulSet`/`StorageClass`/node-pool/reservation/quota reads
   every other collector already reads with confidence. The remaining two held out longer:
   `spot-scarcity-risk` reads a beta Spot capacity-advice API and `autoscaler-out-of-resources`
   parses `jsonPayload` out of an internal autoscaler-visibility log schema, and while neither
   shape had been verified anywhere in this repository, encoding a guess as tested code would
   have made the guess look like a fact. Both were read live against `adamparco-kage` on
   2026-08-29 and converted against captured responses — which immediately paid for itself: the
   log schema turned out to write a stockout under **two** `jsonPayload` shapes, and the
   `value(...)` projection the SOP had been carrying reads only one of them. What remains
   uncovered is two sub-conditions, 3.10(b) and 3.12(b), named in the SOP and in the collector's
   own docstring. Phase 4 is complete.
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
7. **Status moves into the store; the ConfigMap is deleted** (§4.5, §4.6) — one PR, and mostly
   deletion. Harness: `phase_path_for` re-pointed at `reports_dir_for(audit_id)/started.json`,
   the three status keys added to the envelope, `_status_post` and its callers removed. Proxy:
   `_handle_fleet_audit_status` and its `do_POST` wiring removed. Chart:
   `templates/fleet-audit-status.yaml` deleted whole — ConfigMap and RBAC together. View:
   re-pointed at the `kubectl exec` projection, with `--pod`/`--container`, the corrected
   Makefile help text, the never/no-store flags, and the non-zero exit that make an unreadable
   store legible. Adds the new `report_status.py` projection. Net negative outside fleet-audit's
   own directory, and it needs no `helm upgrade` to take effect — which is the property whose
   absence caused the failure it fixes.
8. **The run lock** (§4.3) — `acquire`/`release`/`prune_tokens` on `started.json`, `start`
   wired to acquire and exit non-zero on a live holder, `finish` wired to release, the
   sub-second ring stamp, §4.3's six anti-wedge covers with `start --steal-lock` as the
   operator override and a nonce-checked release on every abnormal exit from `main`,
   the degradation to an unlocked run when the store cannot be written, and
   the multi-process lock tests. Separable from 7 and shipped after it, because it changes when
   a run _refuses to start_ and deserves its own review and its own live validation rather than
   riding along with a deletion PR.
9. **The reader skill** (§4.9) — `agents/platform/skills/fleet-audit-reports/` with its
   `SKILL.md` and `report_query.py`, the reading section moved out of `fleet-audit`'s SKILL.md
   and replaced by a pointer, one line in `generate_docs.py`'s `SKILL_GROUPS`, and
   `make docs-generate` for the skill catalogue. No harness change at all — it reads files
   phases 6 and 7 already write.

## 11. Files touched

| Area      | Files                                                                                                                                                                                                                                                                                                                                                                                    |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Harness   | `agents/platform/skills/fleet-audit/scripts/audit_report.py` (t0, duration keys, manifest cross-check, status writer, label caching), `test_audit_report.py`                                                                                                                                                                                                                             |
| Collector | `agents/platform/skills/fleet-audit/scripts/collect.py` (obtainability, compliance, ai-security check tables), `patch_readiness.py`, `fleet_waste.py`, `fleet_drift.py`, `fleet_stockout.py` (all new), `agents/platform/skills/gcp-networking-fabric-audit/scripts/networking_audit.py` (extended from a PSC-only helper to the stream's full roster), tests for all of the above (new) |
| SOPs      | all eight in `agents/platform/governance/` (shrink per §7), `agents/platform/cron/jobs.json` (prompts)                                                                                                                                                                                                                                                                                   |
| Proxy     | `agents/platform/scripts/credential_proxy.py` — the `_handle_fleet_audit_status` route added in phase 2 and **removed again in phase 7**, with its tests; `command_policy.py` untouched throughout                                                                                                                                                                                       |
| Chart     | `charts/kube-agents/templates/fleet-audit-status.yaml` — added in phase 2, **deleted in phase 7**. No chart object, no RBAC, and no operator change survives this design                                                                                                                                                                                                                 |
| Status    | (§4.5, phase 7) `audit_report.py` — `started.json` relocation, three envelope keys, `_status_post` deleted; `agents/platform/skills/fleet-audit/scripts/report_status.py` (new projection); `test_audit_report.py`                                                                                                                                                                       |
| Lock      | (§4.3, phase 8) `audit_report.py` — `acquire`/`release`/`prune_tokens` over `started.json`, `start` wired to acquire and `finish` to release, the six anti-wedge covers and the `--steal-lock` override, sub-second ring stamp; the multi-process lock tests in `test_audit_report.py`                                                                                                   |
| Store     | (§4.8, phase 6) `audit_report.py` — store writer, delta re-pointed at the store, ledger-body read-back deleted; `test_audit_report.py`; the fleet-audit `SKILL.md` reading section; `fleet-audit-issue-ledger.md`'s delta-memory amendment; `concepts/declarative-workflow.md`'s computable-delta bullet                                                                                 |
| View      | `scripts/fleet_audit_status_view.py` (new), `Makefile`, tests (new)                                                                                                                                                                                                                                                                                                                      |
| Skills    | (§4.9, phase 9) `agents/platform/skills/fleet-audit-reports/SKILL.md` and its `scripts/report_query.py` (both new); the reading section cut from `fleet-audit`'s `SKILL.md` and replaced by a pointer; one line in `scripts/generate_docs.py`'s `SKILL_GROUPS`; `tests/` coverage for the query script                                                                                   |
| Docs      | this file's row in `docs/README.md`; `fleet-audit-issue-ledger.md` §6 exit-contract sentence; SKILL.md payload/field-count text; the stale stream-count pages named in §10 phase 5; generated regions via `make docs-generate`                                                                                                                                                           |

## 12. Questions, resolved

**Q1. Why not fix the speed by telling the model to batch, and skip the collector?**
_Resolved:_ see §4.2's first rejected alternative. The measured failure history of this feature
is almost entirely "the model did not do what the prose said" — the fix is less prose-executed
work, not better prose.

**Q2. Does the status surface contradict the design of record's "no state outside GitHub"?**
_Resolved:_ no, by construction — §4.5 and the first new red line. What §14 rejected was state
the audit's semantics depend on; these files are telemetry no audit decision reads, and deleting
them changes no audit behaviour. Where this design does deviate from §14 — a harness-written
record instead of the operator alerting §14 pointed at — §4.5 says so openly and keeps the
operator path compatible. The answer did not change when the surface moved from a ConfigMap to
files; if anything it got easier to hold, because a file on the pod's own volume is further from
"state outside GitHub" than an object in the cluster's etcd.

**Q3. Can triage overlap collection across shell calls?**
_Resolved:_ deferred — see §4.3's rejected alternative. One live test of cross-call
backgrounding can revisit it; nothing in the design precludes it later.

**Q4. Why does the harness write the status files instead of the cron layer that already has
`started_at`/`finished_at`?**
_Resolved:_ two reasons, and the second is decisive. The executions ledger is Hermes-generic, so
teaching the cron layer audit-specific fields (findings, PRs, issue URLs) would smear the feature
across a patched third-party module, while the harness holds every field at exactly the moment it
is true. More importantly, **the ledger does not see every run**: a stream dispatched from a
kanban card never reaches the scheduler, and one such run finishing 2026-08-27 21:40:45Z is
absent from `executions.db` altogether. A liveness surface blind to a dispatch path would report
a stream that ran as never having run. `started.json` is written by the harness on every
dispatch, so it covers both. The cron envelope itself stays on-pod (§4.4, §13).

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
  `stockout-prevention`'s 3.10(b) and 3.12(b) sub-conditions, and any cluster on the manual
  fallback after a gate failure. _Why accepted:_ both sub-conditions turn on GKE CRD and
  reservation state this repository has not exercised, the same bar the stream's other two
  holdouts cleared on 2026-08-29 before they were converted; the manual fallback is deliberate
  (§6). _What it costs:_ the 2026-08-03 class stays possible in exactly those corners. _What
  would change it:_ verifying those shapes against a live cluster and converting them too.
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
  cron executions ledger on the PVC; the status files start at the harness's t0. _Why accepted:_
  the cron layer would have to grow an audit-specific surface to expose it (Q4), and it does not
  see kanban-dispatched runs anyway. _What it costs:_ pre-`start` overhead is invisible in the
  view. _What would change it:_ the executions ledger gaining a projection of its own, which the
  view's exec channel could then read alongside the store.
- **The view now needs `kubectl exec`, not just `get`.** Reading the store means execing into
  the agent pod (§4.6). _Why accepted:_ the ConfigMap's off-pod readability was theoretical —
  it bought nothing across the thirty hours the object did not exist — and a pod that refuses
  exec is a louder signal than a stale table. _What it costs:_ a reader with read-only RBAC and
  no `pods/exec` cannot run the view, and the view cannot report on a pod that is `Pending` or
  `CrashLoopBackOff`. _What would change it:_ a genuine off-pod consumer, at which point the
  right shape is a projection published by something that already runs off-pod, not a second
  write path from the harness.
- **The store can be deleted or corrupted by an operator, and now takes run health with it.**
  _Why accepted:_ it is still telemetry no audit decision reads, and the next run's write
  restores it. _What it costs:_ more than the ConfigMap's wipe did — the last row and the
  liveness stamps go together, so a wiped store reads as "never ran" for every stream until each
  next run. _What limits it:_ §4.6's `NEVER` and `NO STORE` flags exist so that state is
  rendered as a distinct condition rather than as a calm empty table.
- **A dead run can hold its stream for up to two hours.** The lock is real — `os.link` onto
  `started.json`, verified atomic on this PVC and proved at 16 processes × 60 rounds (§4.3) — so
  the concurrency this replaces is no longer a risk. What replaces it is the ceiling: a run
  killed by an OOM or a pod eviction leaves a claim that nothing releases. Five of §4.3's six
  covers narrow the window to nothing for the deaths that actually happen — a claim written by a
  container that has since restarted is dead on sight, as are a future-dated and an unreadable
  one, and a `start` that fails after claiming the stream hands it straight back — so the full
  two hours is only reached when the run died _inside a pod that is still running_ and _past
  `start`_, which is the case where a concurrent dispatch is genuinely dangerous. _Why
  accepted:_ two hours is longer than any observed run and shorter than every stream's schedule,
  so the window closes on its own before the next scheduled dispatch, and a refusal that names
  the dead holder's pid and start time is a better failure than two runs publishing over each
  other. _What it costs:_ a manual retry inside that window is refused. _What limits it:_
  `start --steal-lock`, the sixth cover — an operator override that runs through the same atomic
  steal, so it cannot itself admit two runs, and the refusal message names it.
- **Steal tokens are litter by design.** A stolen dead claim leaves `.steal-<nonce>` behind
  permanently, because deleting it on success is exactly the bug the torture test caught: a
  racer holding the same dead claim would find the token free and replace the new owner (§4.3).
  _Why accepted:_ correctness beats tidiness, and the file is empty-ish and one per steal.
  _What it costs:_ a few dozen bytes per crashed run, pruned by `prune_tokens` once older than
  the ceiling. _What would change it:_ nothing — the prune is the answer, and it is in phase 8.
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
