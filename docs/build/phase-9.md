# Phase 9 — The Action Broker, dark (task breakdown)

Source of truth: [`docs/design/07-implementation-roadmap.md`](../design/07-implementation-roadmap.md)
§2 "Phase 9 — The Action Broker, dark". Conformance spec:
[`docs/design/09-verification-and-validation.md`](../design/09-verification-and-validation.md).
Contracts: [06](../design/06-api-and-data-contracts.md) §4 (action contracts), §2.2.1 (broker
operations grant); [03](../design/03-security-model.md) §4–§6; [08](../design/08-agent-runtime-and-identity.md)
§2 (the workload pair).

**Goal (07 §2, verbatim intent):** build the entire safety machinery — broker, envelope, classifier,
journal, undo, brake — and exercise it end-to-end **with no write authority anywhere**. The actor
ServiceAccounts are created but bound to empty roles; the broker runs every action in dry-run.

**Why this shape:** the hardest and most novel code in the project lands, gets reviewed, and gets
tested against real clusters while the worst possible bug is still a no-op. Phase 10 becomes a
permission change rather than a leap.

This is the largest phase in the roadmap by a wide margin: it introduces five CRDs, a second binary,
a second image, a second Deployment per `Agent` CR, and four BLOCKING-ALWAYS suites at once. It is
planned as ten units, and the sequencing below is load-bearing — three of the four planning defects
found in this pass are ordering or scoping problems that would otherwise have surfaced at the gate.

---

## ▶️ RUNNING AGAIN, STILL OPEN — read this before resuming

> **2026-07-31, the current state.** The **task ladder is finished** — 72 leaf units, 2 moved out of
> the phase, 70 in-phase, all 70 done — and **the phase still cannot close.** `harness-milestone` was
> invoked and stopped at its §1: **14 of the 55 required Phase 9 checks are asserted by nothing in
> this tree**, 7 of them BLOCKING-ALWAYS. That is planning defect 4 arriving exactly as it predicted,
> because its declared resolution ("`verify-phase9.sh` runs the ratchet, not the Accept list") was
> written into the acceptance table and never into the script. The audit, the fourteen IDs and the
> four units that close them are the last section of this file: **§ Milestone audit 2026-07-31**.
> **`P9-T11a`, `P9-T11b-1` and `P9-T11a-2` are done (2026-07-31)** — the ratchet arm exists, and
> deriving the requirement properly from 09 §10 rather than from the hand-written table restated the
> gap against a denominator of **75**. `T11b-1` then took **V-ISO-001** and **V-ISO-002** green at L2
> and, in doing so, found that the arm **under-counted green**: `parse_results` keyed on the raw
> `check_id` cell, and **36 of the 160 rows in `results.csv` name more than one ID**, so every grouped
> row was invisible to it — 10 IDs falsely reported unasserted, two of them `T11b-1`'s own, which is
> why the fix was split out under Guardrail 9. `T11a-2` split the cell on the ID pattern and gave the
> control the grouped-row cases it had never had: the arm now prints **28 not green / 12
> BLOCKING-ALWAYS**, and that figure is the real worklist. **`P9-T11b-2` is done too** — V-ISO-006
> green at L2, and not by the route the plan row described: binding arm B to the ID would have
> recorded a green with CH6's recovery clause unasserted, so arm C was added and the arm now proves
> the journal comes back without a broker restart. **`P9-T11a-3` is done** — researching `T11c` found
> that all four of its check IDs carry 09 §6 Phase **10**, because the arm was reading §10's ratchet
> table while ignoring §6's Phase column ("the roadmap phase _by which it must be green_") and
> reading only ONE row of §10, though §10 opens _"once a suite enters the ratchet it never leaves"_.
> Both halves are fixed and they pull opposite ways: 21 IDs out, **31 in**, ratchet 70 → **80**,
> required 75 → **98**, and the gate got HARDER — **34 not green / 19 BLOCKING-ALWAYS**.
> **`P9-T11c″` and `P9-T11c′` are done, in that order.** `T11c′` corrected this file's own
> acceptance table, which demanded 16 IDs 09 §6 dates to phase 10, 14 or 15 — the required set is the
> UNION of §10 and the table, so the table kept every one of them required. Retargeted, in their own
> `##` section so the record and the requirement do not share a parse: required **98 → 82**, not
> green **34 → 22**, BLOCKING-ALWAYS **19 → 11**. It also found two things nobody was looking for:
> **17 of the 251 check IDs 09 mentions have no §6 catalog row** (three BLOCKING-ALWAYS), and **ten
> required checks have no `results.csv` row at all**, nine BLOCKING-ALWAYS, every one of them a
> phase-8 ratchet member — a phase that closed. It also broke the ratchet arm's negative control, so
> it was reverted and **`T11c″` landed first**: three of the control's phase-filter cases picked
> their victim from _"required but not named by the table"_ and went unstageable the moment the table
> moved. A check split off under Guardrail 9 goes **before** the artifact that motivated it, not
> after. **`P9-T11c‴` is done** — the other half of the same correction: the 43 IDs 09 §10 requires
> at phase 9 that the table never named are now eight themed _(ratchet only)_ rows, property 4 is
> silent, and the required set did not move (82 → 82) because nothing was added to the gate, the gate
> was written down. **`P9-T11f` is done** — the 17 uncatalogued check IDs now have rows (a new 09
> §6.15 for the fourteen V-CMP, three appended to the §8 V-MET table), and this time the required set
> **did** move: **82 → 91** required, not green **22 → 26**, BLOCKING-ALWAYS not green **11 → 13** —
> and the unit's own V-MET-010 run then closed one of the four it exposed, ending at **25 not green,
> 12 BLOCKING-ALWAYS**. **`P9-T11g-1` is done** — and it found that `T11g`'s premise was wrong.
> `T11g` was scheduled as _"eleven runs to record and two builds"_; auditing all thirteen showed
> **one run and twelve builds**. Only V-MET-012 was implemented-and-unrecorded. `T11g-1` recorded it,
> built the CRD-authority lint that closes **V-CTR-003 and V-CMP-011** together, and split the rest
> into `T11g-2/3/4`: **25 → 22 not green, 12 → 11 BLOCKING-ALWAYS**. **`P9-T11g-2a` is done** — and
> `T11g-2` split again, because its "one tool over `verification/traceability.yaml`" pointed at a
> file that answers a different question — that one maps 01–08's **177 Verification bullets** and is
> V-MET-011's, while 09 §8 asks for a mapping over **every normative statement**, ~538 of them, in an
> `R-<doc>.<section>-<n>` ID space that has never been minted. `2a` built the link that _was_ a session's work:
> `verification/implementations.yaml`, a curated `check-id → {runs, asserts_in}` registry, and the
> V-MET-001 lint over it — which found V-CTR-001 green with nothing asserting it, two phase-12 rows
> passing on spec citations rather than a command, and seven green checks whose implementation named
> its own ID nowhere. **22 → 21 not green, 11 → 10 BLOCKING-ALWAYS**. **Resume at `harness-run`, unit
> `P9-T11g-2b`** — V-MET-002/008/009, which need that requirement enumeration first. Then `T11g-3`,
> `T11g-4`, `T11d`. Do not re-run `harness-milestone` until the T11 ladder is green.

**Phase 9 is OPEN.** It was stopped here on 2026-07-30 by an explicit human instruction, after the
unit `P9-T9b-5b-0-ii-a`, and **not** because the phase closed. The same person lifted the stop later
the same day — _"run the harness until completion, start with the failed PR #83"_ — which also
cleared the one thing that had gone red in the interim.

**What went red, and what cleared it.** PR #83's first CI run turned **V-BRK-023** (L1,
BLOCKING-ALWAYS) red: `TestCreateDropsStatusAndKeepsThePhaseLabel` asserted a premise about
`client.Create` that a later commit correctly made false. That was an open halt, and a halt is
cleared by a human, never by the harness. The instruction above is the clearance; the fix is
`P9-T9b-5b-0-ii-a-fix`, in which `writeahead.Confirmer`'s phase arm reads **both** `status.phase` and
its label and refuses their divergence as a state of its own. Full narrative in the Halt row of
[`LEDGER.md`](LEDGER.md) and in `verification/results.csv` rows 146 (the fail) and 147 (the
correction). Nothing is red now, nothing is deferred.

The `phase-9-a-real-caller-at-the-door` branch was pushed and merged so that a large body of
verified work would not sit stranded on a branch. **A merged phase branch is the normal signal that
a phase closed, and here it means nothing of the kind.** `harness-milestone` was deliberately not
invoked, so the 09 §10 phase ratchet and the `L2_CHAIN_FLOOR` lowering are both unmoved and both
unearned. Do not back-fill them.

An **improvement pass is due before the next unit**: `binding.md` §Thresholds schedules one _"after
any halt is cleared"_. It has [[LSN-054]] and [[LSN-055]] queued, and LSN-054 is a correction to
[[LSN-052]]'s proposed mechanization rather than an addition to it.

**`P9-T9b-5b-0-ii-b` landed 2026-07-31** — the three read-only per-tier actor templates, their
bindings, `render_actor_grant`, the wiring, and the `v in ['get', 'list', 'watch'] ||` disjunct in
all three copies of `vap-agent-readonly`. V-BRK-013's tier arm now runs against the real tree rather
than a synthetic, at the profiles 5b-0-ii-a computed a unit earlier (83 / 89 / 68 against ceilings of
171 / 172 / 136), with developer-team a `Role` and never a `ClusterRole`. Its section is at the end
of this file.

**`P9-T9b-5b-0-iii` landed 2026-07-31** — `dev/verify/broker-execute-l2.sh` is **10/10, rc 0,
PROVEN**, and **V-BRK-006** (L2 clause) and **V-REV-001** (n=1) are scored `pass` in
`verification/results.csv`. Its section is at the end of this file. It cost three defects that had
been invisible for five phases; see [[LSN-060]] and [[LSN-061]].

**`P9-T9b-5b-ii-a` landed 2026-07-31** — `dev/verify/broker-refuse-l2.sh` is **14/14, rc 0,
PROVEN**: V-BRK-018 at L2 and the journal half of acceptance (d). `T9b-5b-ii` was split at SELECT
into `ii-a` (the two refusals) and `ii-b` (V-REV-003 and V-BRK-021's surface scan); its section is
at the end of this file.

**`P9-T9b-5b-ii-b-1` landed 2026-07-31** — `dev/verify/broker-gate-l2.sh` is **16/16, rc 0,
PROVEN** (negative control 39/39): **V-REV-003** at L2, the gated outcome neither the accepting line
nor the refusing one can reach. `ii-b` was split at SELECT into `ii-b-1` (V-REV-003) and `ii-b-2`
(V-BRK-021's surface scan **plus** the `verify-phase9.sh` §G retarget, which is a false pass today);
its section is at the end of this file.

**`P9-T9b-5b-ii-b-2` landed 2026-07-31** — `dev/verify/broker-auth-l2.sh` is **21/21, rc 0, PROVEN**
(negative control 20/20): **V-BRK-021** at L2, the surface of the binary the controller handed out.
Seven arms — nineteen non-routes 404, eight methods 405, three query parameters 400, all ten of
`server.go`'s bypass headers 400 on an unauthenticated route with no token presented, the 200
differential that makes those ten attributable to the headers, one reachable port of eight dialled,
and the three-writer declared-port surface. The unit also closed `verify-phase9.sh` §G's V-BRK-021
detector, **a false pass since it was written**: it discovered its claimant with
`grep -l 'V-BRK-021' dev/verify/*-l2.sh`, and the tree's one match was `broker-refuse-l2.sh`'s
comment saying it does _not_ carry the property. Its section is at the end of this file.

**`P9-T9b-5c` landed 2026-07-31** — `dev/verify/actor-grant-sweep-l2.sh` is **13/13, rc 0, PROVEN**
(negative control 16/16): **V-BRK-013** at L2, and with it Phase 9 acceptance bullet **(e)**. 647
questions derived from 06 by the L0 check's own parser and asked of the live authorizer over all six
labelled identities in three tiers — **204/204** grants held, **434/434** verbs refused, in one
transcript, because a one-sided sweep passes perfectly against a fleet whose RBAC never applied. The
`¬` is on the cluster: an **unlabelled** Role, invisible to both the L0 check and the VAP and fully
effective, made A-4 and A-5 go red by name. Its section is at the end of this file.

**`P9-T8b-4b-ii-2b-ii` was split at SELECT on 2026-07-31** into **`2b-ii-a`** (the derived soak
corpus, L0) and **`2b-ii-b`** (the soak itself, V-REV-001 at L2); the split and its reasoning are in
its own section at the end of this file. **`2b-ii-a` landed 2026-07-31** — `dev/verify/fixtures/soak_corpus.py`
derives **37 envelopes** from the 181-case classifier corpus, filtered by what the shipped write
overlay authorizes, `--self-test` **17/17** and a new L0 chain line; V-REV-001's denominator is no
longer 1. **`2b-ii-b` landed 2026-07-31** — `dev/verify/undo-coverage-l2.sh` submits all 37 envelopes
through the broker's real front door and scores **V-REV-001 at 35/35 = 100% across 3 verbs**, `¬`
**17/17**, one new line in each chain. **`P9-T9c` was appended 2026-07-31** by the
ORIENT drain of `BACKLOG.md` B-006 — 06 §4.4 row 3's auto-pause has no consumer; its section is at
the end of this file. **`P9-T9c` was split at IMPLEMENT on 2026-07-31** into **`-1`** (the row-3
auto-pause consumer) and **`-2`** (a writer for `status.broker.journalReachable`), because only the
first had a seam to wire — see its section. **`-1` landed 2026-07-31**: the refusal that row 3
produces now carries its own `AutoPause` to the HTTP boundary, where it is recorded on the refusal's
own `ActionRecord` through the same `escalate.Recorder.Pause` seam row 9 uses; 4/4 mutants caught.
**`-2` landed 2026-07-31**: `status.broker.journalReachable` is now written by the operator — the
only principal that may, since no broker grant reaches `agents/status` — from three conjoined
observations against the etcd 05 §1.2 puts the journal in, refreshed on a 60 s clock because the
field has no watch behind it; 8/8 mutants caught. B-006 is closed on both halves.
`P9-T9c` was the last task in the ladder as planned — and the ladder was not the whole phase.
`harness-milestone` ran, stopped at §1, and opened **`P9-T11a`–`d`**; `T11a`, `T11b-1`, `T11a-2`,
`T11b-2`, `T11a-3`, `T11c″`, `T11c′`, `T11c‴`, `T11f`, `T11g-1` and `T11g-2a` all closed the same
day, and `T11c′` opened four more — `T11c″` (which it then had to wait for), `T11c‴`, `T11f` and
`T11g`. `T11g` then split at ORIENT into `T11g-1`–`-4`, because its audit found twelve builds where
the row had promised eleven runs, and `T11g-2` split again at IMPLEMENT into `-2a`/`-2b`, because it
was scheduled over three artifacts that do not exist. **Resume at `harness-run`, unit `P9-T11g-2b`**
(§ Milestone audit 2026-07-31, at the end of this file).

The full resume point, including what comes after 5b-0-ii-b, is in the Current task cell of
[`LEDGER.md`](LEDGER.md).

---

## Survey of the current state — Phase 9 is greenfield

Unlike Phase 8, which repaired things that existed and were wrong, Phase 9 builds things that do not
exist at all. The survey is therefore short, and its value is in being explicit about the absence, so
that "extend X" never gets planned against an X that is not there.

### Nothing of the action pipeline exists

| Artifact                   | Expected by           | Present today                                                                                        |
| -------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------- |
| `ActionRecord` CRD         | 06 §4.3               | **absent** — `k8s-operator/config/crd/bases/` holds exactly one CRD, `…_agents.yaml`                 |
| `ChangePolicy` CRD         | 06 §4.2               | **absent**                                                                                           |
| `FleetFreeze` CRD          | 06 §4.4               | **absent**                                                                                           |
| `UndoRequest` CRD          | 06 §4.4               | **absent**                                                                                           |
| `ApprovalRoster` CRD       | 06 §4.4               | **absent**                                                                                           |
| Broker package             | 08 §2.1               | **absent** — `k8s-operator/internal/` is `agentindex controller eventingress router testing webhook` |
| Broker binary              | 08 §2.1               | **absent** — `k8s-operator/cmd/` is `main.go eventingress k8s-event-watcher router`                  |
| Broker image               | 08 §2.1               | **absent** — seven first-party images publish today; `kube-agents-broker` is not one                 |
| `spec.operations` on Agent | 06 §1.1               | **absent** — no `Operations`/`Paused`/`DryRun` identifier anywhere in `agent_types.go` (164 lines)   |
| `status.broker` on Agent   | 06 §1.1               | **absent**                                                                                           |
| Undo controller            | 05 §1.3, 09 §5 `C-UC` | **absent**                                                                                           |
| Journal reconciler         | 09 §5 `C-JR`          | **absent**                                                                                           |
| Classifier corpus          | 09 §7.1               | **absent** — `verification/` holds `traceability.yaml` and `results.csv`                             |

The single occurrence of the word "broker" in the operator tree is a comment at
[agent_webhook.go:463](../../k8s-operator/internal/webhook/agent_webhook.go#L463), documenting that
`status.broker.actorServiceAccount` is status rather than spec. It describes a field that does not
yet exist; P9-T1/T7 make it real. `internal/router/classify.go` is chat-event classification and is
**not** related to risk classification — the name collision is a trap for a future reader and P9-T3
must not extend it.

### What Phase 8 leaves in place, and that Phase 9 builds onto

- The `Agent` CRD, its cardinality/scope/ceiling webhook (V-1…V-10, all ten enforced), and its
  goldens — `internal/controller/agent_manifests.go` is the render site and is golden-tested.
- Per-tier egress NetworkPolicy, tenant quota, and the namespace default-deny, all applied from the
  install path. The broker's `8443` ingress rule and the agent's egress-to-broker rule are new holes
  that must be punched deliberately, in the same templates (P9-T7).
- `dev/lib/preconditions.sh` P1–P10, `dev/L0-CHAIN.txt` (14 lines), `dev/L2-CHAIN.txt` (7 lines),
  `dev/tests/invariants-gate.py` (14 checks), and `dev/cluster/reload-images.sh` deploy-by-digest.
- `verification/traceability.yaml` **already cites the Phase 9 check IDs** — V-BRK-001…005,
  V-REV-001/002, V-GAT-003/005, V-RUN-001/002/004/009/013/014 all appear as mappings for bullets in
  02 §10, 03 §11, 05 §8 and 08 §7. The matrix is not a Phase 9 deliverable; making those IDs
  _executable_ is.

### The one live defect this phase carries (P9-T10)

`agent_common` is the MCP server implementing `call_agent`, the inter-agent transport. It is
declared inconsistently in the two definition sites and gets no credential in either:

- **Image-baked** `agents/platform/config.yaml` (and the two peer tiers): the `mcp_servers:` block
  declares **only** `platform_control`. `agent_common` is absent from `mcp_servers` entirely while
  being listed in both `platform_toolsets.cli` and `platform_toolsets.api_server`
  ([config.yaml:2-34](../../agents/platform/config.yaml#L2-L34)).
- **Runtime-authoritative** `renderConfigYAML()`: `agent_common` **is** declared, with `command` and
  `args` and **no `env:` block at all**
  ([agent_manifests.go:156-159](../../k8s-operator/internal/controller/agent_manifests.go#L156-L159)),
  while `platform_control` beside it declares six variables including `API_SERVER_KEY`
  ([config.yaml:10-16](../../agents/platform/config.yaml#L10-L16)).

Hermes passes an MCP server only what its config declares, so `agent_common_server.py` reads an
empty key and refuses every inter-agent request with `ERROR [500]: API_SERVER_KEY is not
configured`. On the live install `/cluster-admin` never answers. **The fail-closed refusal is
correct and must not be weakened** — the defect is the missing env, not the refusal.

---

## Planning defect 1: "bound to empty roles" is literally incompatible with journalling

07 §2 says the actor ServiceAccounts are "created but bound to **empty** roles". 06 §2.2.1 says every
actor identity additionally receives the **broker operations grant**, byte-identical across tiers —
`create` on `tokenreviews`, `get/list/watch/create` on `actionrecords`, `get/update/patch` on
`actionrecords/status`, `get/list/watch` on `fleetfreezes`, `agents`, `changepolicies` and
`approvalrosters`. Without it the broker cannot authenticate its caller (pipeline step 1), cannot
read the brake (step 5), and cannot write the journal (step 11) — which is precisely the thing
Phase 9 exists to exercise. 06 §2.2.1 states the consequence explicitly: a tier that cannot read
`fleetfreezes` "fails closed permanently … so omitting this grant does not fail safe — it bricks the
tier."

**Resolution — "empty" means empty of _tenant_ authority, and the phase asserts that in both
directions.** The Phase 9 actor Role/ClusterRole is **exactly** the 06 §2.2.1 grant and nothing else.
Accept (e)'s `auth can-i` sweep is therefore not "no write verb, full stop"; it is:

1. **negative** — no agent identity holds any write verb on any resource outside the grant's own
   resource set; and
2. **positive** — every actor identity holds exactly the grant, and holds **no** `update` or
   `delete` on `actionrecords` (the append-only property).

That is V-BRK-013 verbatim ("asserted in both directions"), which is already in the extended catalog
at 09 §6.14 and already assigned to Phase 9. The two-sided form is what makes the sweep falsifiable:
a one-sided "no write verbs" sweep passes on a broken install where the actor role is genuinely
empty and the broker has been fail-closed since boot.

**Consequence for the ledger:** Accept (e) is bound to V-BRK-013 in addition to V-CTN-004, and the
sweep script asserts the exclusion set by name rather than by "these are the ones that were there
when I wrote it".

---

## Planning defect 2: V-BRK and V-REV are BLOCKING-ALWAYS and half of them require a real write

09 §6 classifies **V-BRK, V-REV and V-ISO as BLOCKING-ALWAYS**; 09 §9.6 says a BLOCKING-ALWAYS check
**may not be deferred**. So "defer the execution-dependent half to Phase 10" is not available — the
gate would refuse the row, exactly as the V-MET-011 deferral was refused in Phase 8.

Sorting the two suites by whether their property needs an actual mutation:

| Dark-mode-native (refusal properties — 9 of 12 V-BRK)                                                                                                                                                                                                                                                                                                             | Needs a real mutation                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| V-BRK-001 (agent container cannot write), 002 (scope spoof), 004 (stripped annotation), 005 (journal down ⇒ refuse), 007/008/009/010 (mTLS, audience, neither-alone, foreign caller), 011 (pipeline order), 012 (one broker per CR), 013 (the grant), 017 (default-audience token), 021 (non-skippability); V-REV-003 (no undo plan ⇒ gated), V-REV-004 at **L1** | V-BRK-003 (journal reconciliation), 006 (write-ahead), 014/015/016; V-REV-001, 002, 005, 006, 007, 008, 009 |

**Resolution — one code path, two RBAC profiles, and the boundary is mechanized.**

- **The shipped profile** is the 06 §2.2.1 grant and nothing else. It is what `provision_*.sh`
  installs, what the live install runs, and what the Accept (e) sweep asserts.
- **A test-only overlay** — `dev/verify/fixtures/actor-tenant-grant.yaml` — adds namespace-scoped
  tenant authority to **one fixture agent** in a dedicated namespace on `gke-scratch-kube-agents-dev`
  so the snapshot → execute → verify → undo half can be exercised for real. It is applied by an L2
  script, torn down at the end of that script, and the Accept (e) sweep runs **after** teardown.
- **The seam is guarded, not trusted.** Three mechanized constraints, added in P9-T9:
  1. `invariants-gate.py` gains a check that no path under `k8s-operator/scripts/`, `deploy/` or
     `config/` references the overlay — it is reachable only from `dev/`. A test-only grant that
     drifts into the install path is the single worst outcome of this decision, so it is the one
     thing a lint refuses rather than a convention discourages.
  2. The fixture broker Deployment is rendered by the **same** `agent_manifests.go` renderer as the
     shipped one, so the fixture cannot become scenery (LSN-024's shape).
  3. Every `ActionRecord` produced under the overlay carries a fixture label, and the L2 script
     asserts the namespace is empty of them at teardown.

**What this buys and what it does not.** It buys genuinely green BLOCKING-ALWAYS suites instead of
vacuous ones — V-REV-001 over an empty population of executed records is a check that cannot fail
(V-MET-014), and shipping one is worse than shipping none. It does **not** prove the path under a
real agent identity driven by a real agent pod; that is Phase 10, and it is stated in the phase-9
results rows rather than implied.

**One reformulation recorded honestly.** V-REV-001 reads "100% of **executed** non-gated
`ActionRecord`s carry a validated undo plan". In shadow mode the fleet's population of executed
records is empty by construction, so the phase-9 instance asserts the same property over records in
the **`DryRun`** terminal phase, and the `results.csv` note says so. The overlay instance asserts it
over genuinely executed records. Both are recorded; neither is described as the other.

---

## Planning defect 3: V-BRK-003 needs an audit-log stream, and it may not be deferred

V-BRK-003 — "every audit-log write by an actor identity has a matching `ActionRecord`" — is L2/L3
and BLOCKING-ALWAYS. GKE does not expose API-server audit configuration to the customer; the stream
lands in Cloud Logging. The scratch cluster is in `adamparco-kage`, a project the harness can read,
so the stream is reachable via `gcloud logging read` — but **Data Access audit logs for the
Kubernetes API are off by default**, and turning them on is a project-level IAM policy change.

**Resolution.** The journal reconciler takes a **pluggable audit source** (interface, not a
hard-coded Cloud Logging client), so the L1 instance runs against a fixture stream and the L2
instance against Cloud Logging. P9-T1 opens with a five-minute probe: `gcloud logging read` for a
known write on the scratch cluster. If the stream is absent, enabling Data Access audit logs for
`k8s_cluster` on `adamparco-kage` is inside the harness's authority and is part of P9-T1 (it is a
scratch project, and the change is additive). The negative control is an injected unjournaled write
by the fixture actor identity under the P9-T9 overlay, which the reconciler must raise. Recorded
here at PLAN time because the alternative — discovering it at the gate — turns a BLOCKING-ALWAYS
check into a milestone-time emergency.

---

## Planning defect 4: Accept (a)–(e) does not cover the ratchet

The 09 §10 ratchet for Phase 9 is **V-BRK, V-REV, V-RUN, V-GAT (L1), V-ISO-001/002/006**. Accept
(a)–(e) covers the envelope round-trip, the classifier corpus, scope spoofing, the brake, and the
`can-i` sweep. It says nothing about **V-RUN** (the workload pair, its identities, labels, hardening,
startup ordering, and `pause`-is-not-scale-to-zero — fourteen checks) or **V-ISO-001/002/006**
(controller down / controller recovered / journal down). A phase closed on Accept alone would leave
seventeen ratchet checks unrun, and 09's Definition of Done requires the ratchet, not the Accept
list.

**Resolution.** The acceptance table below carries explicit **ratchet-only rows** with no Accept
bullet, marked as such. `verify-phase9.sh` runs the ratchet, not the Accept list; Accept is the
subset that 07 chose to name. This is stated because "Accept is green" reading as "the phase is
done" is 09 §11.8's failure mode with a different label.

Two smaller notes in the same family:

- **07 §2's task table lists P9-T10 before P9-T9.** The gate task is genuinely last; the numbering is
  a source-document ordering artifact and the sequencing below uses dependency order, not the
  printed order.
- **P9-T10 binds to V-CMP-006, which is not in the Phase 9 ratchet.** It is a live-install defect
  given a phase-9 slot by 07 §2 because that is when it was found. It has its own check and its own
  L2/L3 evidence, and it does not gate on any broker work — which is why it goes first.

---

## Acceptance → check binding (07 §2 "Accept", plus the 09 §10 ratchet)

Every bullet binds to at least one check ID. No bullet is unbound. The rows below the Accept bullets
are ratchet obligations with no corresponding Accept bullet — see planning defect 4.

**Corrected 2026-07-31, in three units, against 09 §6's Phase column.** The table's first draft was
wrong in both directions: it named sixteen IDs that 09 §6 dates to phase 10, 14 or 15, and it omitted
forty-three that 09 §10 requires at phase 9. Neither was visible while the required set was a hand
list. `P9-T11c′` removed the sixteen; **`P9-T11c‴` added the forty-three, and the table is now
complete** — `dev/tests/phase-ratchet-is-asserted.py --phase 9` reports property 4 silent, and the
required set did not move (82 before, 82 after), because every one of the forty-three was already in
the ratchet and only the phase file's own account of it was short. The reason the two halves are two
units is in [_P9-T11c′_](#p9-t11c--the-sixteen-that-were-not-phase-9s--2026-07-31-) below.

**Then `P9-T11f` moved it, and moving it was the point.** Giving 09 §6 a catalog row for the
seventeen IDs it defined only in prose put ten of them into the suite expansion, and the two rows
this table needed to stay complete are the measurement row (now `V-MET-001…014`) and a new
completeness row. The gate got **bigger**: required **82 → 91**, not green **22 → 26**, of those
BLOCKING-ALWAYS **11 → 13** — then the unit recorded its own V-MET-010 run and it closed at **25 not
green, 12 BLOCKING-ALWAYS**. Unlike `P9-T11c‴`, where an unmoved required set was the evidence the
unit was bookkeeping, here the movement **is** the result — four checks that a phase-9 gate could
not previously fail on are now checks it can, and V-CMP-011 and V-CMP-020 are the two of them with
no implementation at all. `P9-T11f`'s own section is
[below](#p9-t11f--the-seventeen-09-defined-only-in-prose--2026-07-31-).

The sixteen are recorded, individually and with their due phase, in
[_Retargeted out of Phase 9_](#retargeted-out-of-phase-9-by-09-6) immediately below; they are named
**there and not here** deliberately, because `dev/tests/phase-ratchet-is-asserted.py` reads every
check ID in **this section** as one the phase requires, and an ID mentioned in a paragraph about its
own postponement would go on being demanded by the paragraph ([[LSN-019]]'s shape, and the same trap
`P9-T11a` fell into once already). Removing them does not unbind a single Accept bullet: (a), (b),
(d) and (e) each keep three or more checks.

| Accept                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Check IDs                                                                                                               | Level      | Target                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | ---------- | ------------------------------- |
| **(a)** an envelope flows end-to-end in shadow mode → well-formed `ActionRecord` + valid undo plan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | V-CTR-005, V-REV-001, V-REV-004, V-BRK-015                                                                              | L1, L2     | dev                             |
| **(b)** classifier matches the fixture corpus (all four classes); `ChangePolicy` tightens, provably cannot loosen                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | V-GAT-001, V-GAT-002, V-GAT-009, V-GAT-010, V-GAT-017                                                                   | L0, L1, L2 | dev                             |
| **(c)** an envelope claiming a scope other than the caller's is rejected                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | **V-BRK-002**, V-BRK-007, V-BRK-008, V-BRK-009, V-BRK-010, V-BRK-017                                                    | L1, L2     | dev                             |
| **(d)** `pause`/`freeze` work with inference down; broker refuses when the journal is unavailable                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | **V-BRK-005**, V-RUN-007, V-RUN-008, V-RUN-012, **V-ISO-006**                                                           | L0, L2     | dev                             |
| **(e)** no agent identity in the fleet holds a write verb — full `auth can-i` sweep                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | **V-CTN-004**, **V-BRK-013**, V-BRK-012                                                                                 | L0, L2     | dev + live (sweep is read-only) |
| _(ratchet only)_ the workload pair, its identities, labels, hardening, ordering                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | V-RUN-001…005, V-RUN-009, V-RUN-010, V-RUN-011                                                                          | L0, L2     | dev                             |
| _(ratchet only)_ journal integrity, write-ahead, pipeline order and non-skippability                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | **V-BRK-006**, **V-BRK-011**, **V-BRK-014**, **V-BRK-021**                                                              | L0, L1, L2 | dev (+ overlay)                 |
| _(ratchet only)_ reversibility beyond coverage: undo-plan correctness                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | **V-REV-003**                                                                                                           | L1, L2     | dev (+ overlay)                 |
| _(ratchet only)_ failure isolation with the pair deployed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | **V-ISO-001**, **V-ISO-002**                                                                                            | L2         | dev                             |
| _(ratchet only)_ the broker pipeline's own properties: every verb of the closed enum end to end, write-ahead as a read rather than a flag, novelty that cannot be switched off by omission, shadow mode that actually shadows, an identity resolved live, and the binary that ships being the one that was tested                                                                                                                                                                                                                                                                                                        | **V-BRK-022**, **V-BRK-023**, **V-BRK-024**, **V-BRK-025**, **V-BRK-026**, **V-BRK-027**                                | L1         | dev                             |
| _(ratchet only)_ the agent↔broker seam: the idempotency key the agent computes, the single write path it is given, the identity it is told it will assume, and the keys a closed decoder accepts                                                                                                                                                                                                                                                                                                                                                                                                                         | **V-BRK-028**, **V-BRK-029**, **V-BRK-030**, **V-BRK-032**                                                              | L0, L1     | tree + dev                      |
| _(ratchet only)_ refusal beats partial work: a permission boundary answers instead of crashing, and a snapshot-persist failure applies neither target                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | **V-BRK-018**, **V-BRK-031**                                                                                            | L1, L2     | dev                             |
| _(ratchet only)_ reversibility beyond the undo plan: a `recreate` downgrade decided from live cluster state, and a rollback that replays the pre-state or refuses                                                                                                                                                                                                                                                                                                                                                                                                                                                        | **V-REV-010**, **V-REV-011**                                                                                            | L1, L2     | dev                             |
| _(ratchet only)_ containment, carried in by 09 §10's phase-8 row: tier-scoped reads, attenuation, `(tier, scope)` cardinality, developer-team placement, a controller that mints no RBAC, and egress default-deny under Workload Identity                                                                                                                                                                                                                                                                                                                                                                                | **V-CTN-001**, **V-CTN-012**, **V-CTN-015**, **V-CTN-016**, **V-CTN-017**, **V-CTN-020**                                | L0, L2, L3 | dev + live                      |
| _(ratchet only)_ a test-only RBAC grant never leaves `dev/`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | **V-CTN-037**                                                                                                           | L0         | tree                            |
| _(ratchet only)_ the CRD contract and the brake: shipped CRs validate and re-apply clean, V-1…V-10 each carry a field-path negative, no authority fields in the schema, the `ActionRecord` lifecycle, the brake fail-closed on an unreadable `FleetFreeze`, 06 §4.4's nine rules as one decision function that refuses on absent input, `C-UC`'s preconditions as one shared predicate, an input source that tells the truth about what it could not read, row 7 asked about the action, no `*_ALLOW_ALL_USERS` escape hatch anywhere in the tree, and an `apply-change` skill that describes the write path that exists | V-CTR-001, V-CTR-002, V-CTR-003, V-CTR-006, V-CTR-007, V-CTR-014, V-CTR-015, V-CTR-016, V-CTR-017, V-CTR-018, V-CTR-020 | L0, L1, L2 | tree + dev                      |
| _(ratchet only)_ the measurement suite — the checks that keep the other checks honest: ID coverage both ways, the traceability matrix in both directions, the two inventories that define "complete", the coverage and assertion ratchets, no reuse or renumbering, classifier/corpus sync, deferrals that name a blocker, no silently-skipped BLOCKING-ALWAYS run, the uncovered list published, the gated-rule set defined once, and negative-control discipline                                                                                                                                                       | **V-MET-001…014**                                                                                                       | L0         | tree                            |
| _(ratchet only)_ completeness, carried in by 09 §10's phase-8 row: every first-party image published, no placeholder in an applied manifest, no authority field in the CRD schema, each tier's skill set exactly its own, every identity the broker names created by the install path, and an overlay that renders the install it claims to                                                                                                                                                                                                                                                                              | V-CMP-002, V-CMP-003, V-CMP-006, V-CMP-007, V-CMP-008, V-CMP-011, V-CMP-020                                             | L0, L2, L3 | tree + dev + live               |
| _(ratchet only)_ the rows **this phase's own coverage draw-down added** to 09 §6, each dated phase 9 and therefore required here: authority never precedes machinery, the developer-team tier has no cloud actor identity, the two path dialects are never interchangeable, a secret digest cannot leave the broker's `classify` package, and the settle windows are the published ones. They reach the required set through 09 §10's suite names exactly as every other row does — naming them here is what keeps property 4 silent as the catalog grows                                                                | **V-CTN-038**, **V-CTN-039**, V-CTR-021, V-GAT-024, **V-REV-012**                                                       | L0         | tree                            |

"dev" is `gke-scratch-kube-agents-dev` — the only destructive-test target. "live" is
`platform-agent-host`, verification only. "overlay" is the test-only tenant grant of planning
defect 2, applied and torn down inside one L2 script. "tree" is a check whose whole property is a
statement about the repository, provable at L0 with no cluster.

V-CTN, V-BRK, V-REV, V-ISO, V-ADV and V-MET are **BLOCKING-ALWAYS**: not one of their rows may close
as `deferred` (09 §9.6). V-GAT, V-RUN, V-CTR and V-CMP are BLOCKING-PHASE and gate the milestone.

**The required set: 96.** `dev/tests/phase-ratchet-is-asserted.py --phase 9` derives it as 95 (09
§10, every row ≤ 9, each suite expanded against §6 and filtered by its member's own due date) ∪ 96
(this table). Exactly one ID comes from the table alone: **V-GAT-002**, which Accept (b) binds and
which no §10 suite name reaches at phase 9. It moved 91 → 96 on 2026-07-31, when the coverage
draw-down of `P9-T11g-2b-ii-2c` added five phase-9 rows to 09 §6 and property 4 caught all five
before they could be required by the ratchet and named nowhere. **The table no longer under-names
the ratchet**:
property 4 is silent, and the union is now the table plus nothing, which is the state planning
defect 4 asked for — a phase file that names every obligation it is closed against, rather than one
that names the subset 07 chose to call "Accept".

---

## Retargeted out of Phase 9 by 09 §6

Sixteen IDs the first draft of the table demanded at phase 9 that 09 §6's Phase column dates later.
They are **not** dropped, weakened, retired or deferred: each keeps its ID, its level, its suite and
its BLOCKING-ALWAYS class, and each is required at the phase named. This section sits outside the
acceptance table on purpose — see the note above it.

| Due | Check IDs                                             | Came from                         |
| --- | ----------------------------------------------------- | --------------------------------- |
| 10  | V-GAT-019                                             | Accept (a)                        |
| 10  | V-GAT-021, V-GAT-022                                  | Accept (b)                        |
| 10  | V-RUN-013                                             | Accept (d)                        |
| 10  | V-BRK-001                                             | Accept (e)                        |
| 10  | V-RUN-006                                             | the workload-pair ratchet row     |
| 10  | V-BRK-003, V-BRK-004, V-BRK-016                       | the journal-integrity ratchet row |
| 10  | V-REV-002, V-REV-005, V-REV-006, V-REV-007, V-REV-009 | the reversibility ratchet row     |
| 14  | V-REV-008                                             | the reversibility ratchet row     |
| 15  | V-RUN-014                                             | the workload-pair ratchet row     |

Ten of the sixteen are BLOCKING-ALWAYS, which is why the retarget is a spec reading and not a
judgement call: **09 §6's own preamble** calls each row's last cell _"the roadmap phase by which it
must be green"_, and 09 §10 opens _"Once a suite enters the ratchet it never leaves"_ — the two
sentences only cohere if a §10 suite name contributes the members §6 dates at or before the phase.
`P9-T11a-3` confirmed that reading three independent ways before acting on it; the working is in
[_P9-T11a-3_](#p9-t11a-3--the-column-the-ratchet-was-not-reading--2026-07-31-) above.

**Four of the sixteen are already green** — V-GAT-021, V-GAT-022, V-REV-006, V-REV-008 — and a green
row is never un-recorded by a retarget: it satisfies the later phase in advance. The other twelve
become **Phase 10's** opening worklist (V-RUN-014 Phase 15's), recorded here rather than left to be
rediscovered. Eight of those twelve are BLOCKING-ALWAYS: V-BRK-001, V-BRK-003, V-BRK-004, V-BRK-016,
V-REV-002, V-REV-005, V-REV-007, V-REV-009.

**This is the one edit in the T11 ladder that makes the gate smaller, so it is stated plainly rather
than buried.** Required goes 98 → 82, not green 34 → 22, BLOCKING-ALWAYS not green 19 → 11. Set
against the figures the ladder started from — 75 required, 27 not green, 11 BLOCKING-ALWAYS — the
required set is still **larger** than the hand list it replaced, and the BLOCKING-ALWAYS worklist is
the same size with **different members**: T11a-3's accumulation of the phase-1–8 §10 rows added
V-CTN-001/012/015/016/017 and V-MET-001/002/008/009, and this correction removed the eight above.
The count being unchanged is a coincidence of arithmetic; the worklist is not the same worklist.

---

## Task breakdown

Ordered by dependency, then by risk. **P9-T10 ships first and alone** — it is independent of every
broker unit, it repairs a defect on a running install, and it is the smallest unit in the phase.

| Task       | What to build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Spec             | Files                                                                                                                                                                                                                                                                        | Check IDs                                                                                         | Weight           |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ---------------- |
| **P9-T10** | Repair the inter-agent credential seam. Declare `agent_common` with an `env:` block carrying `API_SERVER_KEY` (and the `KUBERNETES_SERVICE_*`/`HERMES_HOME` set `platform_control` gets) in **both** definition sites, for **all three tiers**; the image-baked config must also stop listing a toolset entry for a server it never declares. Bind to **V-CMP-006** with a lint that fails any MCP server whose script reads a credential from the environment and whose config declares no `env`. **Do not weaken the fail-closed refusal.** Record for P15-T1: the per-tier `API_SERVER_KEY` values currently differ and `resolve_agent_credentials` sends the caller's own key as the target's bearer.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 05 §1; 06 §4.1   | `agents/{platform,cluster-admin,developer-team}/config.yaml` · `k8s-operator/internal/controller/agent_manifests.go:156` · goldens · new `dev/test_mcp_env_declared.py` · L0-CHAIN                                                                                           | **V-CMP-006**                                                                                     | medium           |
| **P9-T1**  | `ActionRecord` CRD + journal store. Full 06 §4.3 schema: attribution, classification, targets, `preState` (with the >1 MiB `objectRef` path), undo plan, the ten-phase status lifecycle, the **two** retention clocks, bidirectional undo linkage, `chainId`. `spec` immutable by CEL; `status` field/principal table enforced by `vap-agent-scope-journal`. Includes the journal reconciler (`C-JR`) behind a **pluggable audit source**, the retention controller's post-export deletion predicate, and the Data Access audit-log probe of planning defect 3.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 06 §4.3          | new `api/v1alpha1/actionrecord_types.go` · `config/crd/bases/…_actionrecords.yaml` · new `internal/journal/` · `internal/controller/journal_reconciler.go` · `internal/controller/retention_controller.go` · `config/policy/vap-agent-scope-journal.yaml`                    | V-BRK-003, V-BRK-015, V-REV-008, V-CTR-\*                                                         | high             |
| **P9-T2**  | Action Envelope + broker skeleton. New tier-neutral binary and image. `POST /v1alpha1/actions` + `GET /healthz` on **8443**, HTTP+JSON over TLS (not gRPC). mTLS **and** projected token with audience `kubeagents-broker`; `TokenReview`; `(tier, scope)` derived from the authenticated caller and **never** from the body. Idempotency key = `"sha256:" + lowerhex(SHA-256(JCS(K)))`, recomputed by the broker. The three anti-replay mechanisms. Exactly one listening port, one mutating route, no `/bin/sh` in the image.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 06 §4.1; 03 §4.1 | new `cmd/broker/main.go` · new `internal/broker/{server,auth,envelope,idempotency}.go` · new `k8s-operator/Dockerfile.broker` · `tags.env` · `deploy/docker/cloudbuild.yaml` · `dev/cluster/reload-images.sh` · publish workflows · `verification/fixtures/envelopes/`       | **V-BRK-002**, V-BRK-007/008/009/010/017, V-BRK-021, V-RUN-010, V-CTR-005                         | **load-bearing** |
| **P9-T3**  | The risk classifier + `ChangePolicy`. Deterministic, table-driven, the 06 §4.2 evaluation order (scope ⇒ short-circuit, forbidden ⇒ short-circuit, max over inputs, `+1` capped at gated, `ChangePolicy` max, no-undo-plan raise). The seventeen code-floor rules including `secret-material-egress` (digest match, **not** entropy), `cross-tier-direct-operation` (ownership computed via the V-6 subset predicate, reused not reimplemented), and the production-label precedence ladder. Both path dialects, with the `/`-prefix rejection at admission. The **120–200 case corpus** of 09 §7.1 with asymmetry pairs. Classifier package imports no inference client.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 03 §5; 06 §4.2   | new `internal/broker/classify/` · new `api/v1alpha1/changepolicy_types.go` · CRD · webhook rule (class ≥ floor) · new `verification/fixtures/classifier-corpus.yaml` · `dev/tests/classifier-corpus-lint.py` (V-MET-005) · L0-CHAIN                                          | **V-GAT-001/002/009/010/017/021/022**, V-GAT-011, V-GAT-012                                       | **load-bearing** |
| **P9-T4**  | Undo-plan generation for every supported verb — the 06 §4.3.1 strategy table (`create`→`delete`, `apply`/`patch`→`restore`, `scale`→`restore`, `delete`→`recreate`, cloud→`inverse`, else `none`), the sanitizer, `preconditions.uid` on every step, inbound-reference detection downgrading `recreate` to `none`, and dry-run validation of each step against the API server. The explicit **"cannot generate" path reclassifies as gated** — this is what makes reversibility true rather than aspirational, so it is tested directly and from both sides. The 09 §7.3 round-trip fixtures including the negative set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 06 §4.3.1        | new `internal/broker/undo/` · `verification/fixtures/undo/` · unit + envtest suites                                                                                                                                                                                          | **V-REV-003**, **V-REV-004**, V-REV-001, V-REV-009                                                | **load-bearing** |
| **P9-T5**  | Snapshot → execute → verify. Server-side apply with field manager **exactly** `kube-agents/<tier>/<scope>`, dry-run first where supported, per-kind verification predicates (04 §5.1), the recovery ladder recorded in `status.recovery`, and the atomicity rule (multi-target: if any snapshot fails, **nothing** is applied). Selector fan-out expanded **once**, before classification, against live state. Write-ahead ordering: the record's durable write precedes the mutation, which precedes the API response, which precedes the chat report.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 04 §1, §5.1      | new `internal/broker/execute/` · `internal/broker/verify/` · envtest suites                                                                                                                                                                                                  | **V-BRK-006**, V-BRK-018, V-BRK-019, V-BRK-020, V-BRK-014, V-REV-002/005/006                      | high             |
| **P9-T6**  | The brake. `Agent.spec.operations` (`paused`, `pauseReason`, `dryRunOnly`, roster/policy refs, initiative budget) and `status.operations`/`status.broker`; cluster-scoped `FleetFreeze`; `UndoRequest`; `ApprovalRoster`; the `contested` index and its advisory annotation; the undo controller (`C-UC`). Every one of the nine fail-closed rules of 06 §4.4. All five controls must work through `kubectl` and the API **with inference down** — no dependency on the model, the router, or the agent pod. `pause` is **not** scale-to-zero.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 03 §6; 06 §4.4   | `api/v1alpha1/agent_types.go` (+`operations`) · new `{fleetfreeze,undorequest,approvalroster}_types.go` · CRDs · `internal/controller/undo_controller.go` · `internal/broker/brake.go` · webhook · goldens                                                                   | **V-RUN-007/008/012/013**, **V-BRK-005**, V-REV-007, V-GAT-003/007                                | **load-bearing** |
| **P9-T7**  | Controller reconciles the pair. Render the broker Deployment, Service (`<agent>-broker`, 8443) and certificate Secret **before** the agent Deployment, both owned by the `Agent` CR; `BrokerReady`/`AgentReady` conditions with `Ready` their conjunction; the `wait-for-broker` init container with observe-and-report on timeout; `KUBEAGENTS_BROKER_ENDPOINT` injection; the `kube-agents/role` label on both halves; the broker's NetworkPolicy (ingress only from `role: reader` with matching `kube-agents/agent`) and the agent's egress-to-broker rule. **Mints no RBAC.** Regenerate goldens.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 08 §2            | `internal/controller/agent_manifests.go` · `internal/controller/agent_controller.go` · `pod_launcher.go` (pair-atomic `LaunchSpec`) · `netpol-*.yaml.template` · goldens · `dev/tests/reference-render.py`                                                                   | **V-RUN-001/002/003/004/005/006/009/011**, V-BRK-012, **V-BRK-011**, **V-BRK-014**, V-ISO-001/002 | high             |
| **P9-T8**  | Shadow mode. The agent's `apply-change` MCP tool submits real envelopes; the broker classifies, plans undo, and journals a `DryRun` `ActionRecord` without calling a mutating API. `dryRunOnly` is stricter-only and cannot be cleared by the agent. Run against `gke-scratch-kube-agents-dev` for the duration of the phase and mine the journal for classifier gaps — every gap found becomes a corpus case, not a code tweak.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 04 §1            | `agents/*/skills/` (the `apply-change` skill) · `deploy/*/scripts/` MCP tool · `internal/broker/server.go` (dry-run terminal path) · a journal-mining note in this file                                                                                                      | V-REV-001 (DryRun scope), V-GAT-019, V-CHR-\* (advisory)                                          | high             |
| **P9-T9a** | **Done 2026-07-30.** The review-gate path filter: `.github/workflows/review-gate.yml` widened from five manifest globs to sixteen, over a security surface **derived** from the repo (kubebuilder RBAC/webhook markers, `tls.Config`/`TokenReview`/`SubjectAccessReview`, and authority-granting manifest kinds) rather than restated; the matcher calibrated against two recorded PR outcomes.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 07 §5            | `.github/workflows/review-gate.yml` · new `dev/test_review_gate_paths.py` · new `verification/mutants/V-MET-007.json`                                                                                                                                                        | **V-MET-007**                                                                                     | **load-bearing** |
| **P9-T9b** | Consolidated gate `dev/verify/verify-phase9.sh`: envelope round-trip, scope-spoof rejection, classifier fixture corpus, undo-plan coverage, brake liveness with inference down, fail-closed on journal loss, and the Accept (e) two-sided `can-i` sweep. The test-only tenant overlay of planning defect 2, with its three guards. Regression through `verify-phase8.sh`. New L0 and L2 chain lines. ~~**Also fix the review-gate path filter, found during P9-T2:**~~ **Split out as P9-T9a and done 2026-07-30** — `.github/workflows/review-gate.yml` triggers on `**/policy/**`, `**/agents/**`, `**/provisioning/**`, `**/namespaces/**` and `**/SOUL.md` — none of which match `k8s-operator/internal/**`, so PR [#33](https://github.com/adamparco/kube-agents/pull/33) added the broker, an authenticator and the one image whose SA can write, and the security gate did not run on it. The gate was written when the security surface was manifests; it now includes Go. Widen the filter to the broker, webhook, router and RBAC paths **in the unit that owns the gate**, not in a unit that would be reviewing itself. Expect the first run to need waiver triage: the suite was tuned on YAML. | 07 §5            | new `dev/verify/verify-phase9.sh` · new `dev/verify/broker-auth-l2.sh` · `dev/verify/broker-execute-l2.sh` · `dev/verify/actor-grant-sweep-l2.sh` · `dev/verify/fixtures/actor-tenant-grant.yaml` · `dev/tests/invariants-gate.py` · `dev/L0-CHAIN.txt` · `dev/L2-CHAIN.txt` | all of the above (V-MET-007 closed by T9a)                                                        | **load-bearing** |

**Every unit ships on its own branch off `origin/main`, its own PR, and is merged and the branch
deleted before the next begins** (`binding.md` §Branching, §Merge). A unit is not "done" until its
PR is merged green.

**P9-T3 ships as two units** (the P8-T8a/b/c precedent), because the row above is two deliverables
that share a heading and nothing else:

- **P9-T3a** — the classifier itself: `internal/broker/classify/`, the shared scope predicate
  extracted to `internal/scope/` and reused by the V-6 webhook rule, the 09 §7.1 corpus, and the two
  L0 lints. Covers V-GAT-001/010/011/012/017/021/022 at L1 and V-MET-005.
- **P9-T3b** — `ChangePolicy`: the CRD, the stricter-only admission rule, the `/`-prefix rejection
  on `fieldPaths`, and the broker taking the max over policy sources. Covers V-GAT-009.

The seam is real rather than administrative: `NewClassifier` already takes `[]RuleSet` and the code
floor is one of them, so T3b adds a source to a list T3a shipped. The order matters in one
direction only — a policy that can tighten needs something to tighten first.

**T3b shipped with one scope boundary worth stating, because it is easy to read as a gap.** Nothing
reads a `ChangePolicy` out of a cluster yet. `FromChangePolicy` converts a CR into the `RuleSet` the
classifier already consumes, and the max-over-sources property is proven against it at L1 — but the
informer that would supply live policies belongs to **P9-T7**, which is where a broker pipeline
first exists to consult one. Wiring a watch into a process that classifies nothing would be
scenery. **V-GAT-009 is therefore claimed at L1 and its L2 instance stays open.**

**T3b's scope note has a T3a sibling that only became visible in T4, and it is a defect rather than
a boundary.** T3a's 165-case corpus was green over a `statefulKinds` list that covered no Config
Connector kind at all, so thirteen irreversible cloud deletes — a database, a bucket, a dataset, two
disk kinds, a GKE cluster — classified `routine` with reason `no rule matched`. Section C of the
corpus tests that list faithfully and could never have found this: **a corpus derived from a list
can only check the list's interior.** What found it was T4's cross-package invariant, which runs the
real classifier over the undo generator's own list of kinds it cannot restore. Fixed at the
definition site with 16 new corpus cases (§M, three of them negative) and recorded as [[lsn-033]];
**V-GAT-001 is re-recorded at L1 as a correction, over 181 cases.**

**T4 claims V-REV-003 and V-REV-004 at L1 only.** V-REV-001 (coverage over executed
`ActionRecord`s) and V-REV-009 (a destructive undo is itself gated) are listed L2-only in 09 §5 and
belong to units that do not exist yet — there is nothing executing and no undo controller to gate.

**A level correction to that sentence, made in T5a rather than inherited.** The paragraph above
originally said the **L2** instances of V-REV-003/004 "need an envtest round-trip against a real API
server, which is P9-T5's". Envtest is **L1** by `binding.md` §Targets — a real API server, but
process-local, no cluster. So the **L2 instances still require `gke-scratch-kube-agents-dev` and
stay open**, assigned to the `broker-execute-l2.sh` line in P9-T9. T5a does not strengthen them at
L1 either: nothing in the executor invokes the undo generator, so no round-trip runs at any level
here — what T5a contributes is the pre-state snapshot those L2 instances will be diffed against.
Recording the correction because the alternative — letting a unit quietly redefine a level to the
one it can reach — is how a phase ratchet stops meaning anything.

**P9-T5 ships as two units**, the same seam as T3a/T3b and P8-T8a/b/c. The row is not one
deliverable: it is the write path and everything that happens after the write, and they fit
together only in the sense that one runs after the other.

- **P9-T5a** — the write path. `internal/broker/execute/`: the field manager (produced in one place,
  with its inverse), the one diff used at both ends of the pipeline, snapshot capture with the
  all-or-nothing rule, the executor's three orderings (dry-run-all-then-mutate, write-ahead journal,
  integrity-before-apply), and the API-server-backed `Reader`/`Applier`. **Claims exactly one check:
  V-BRK-020 at L1**, which is the only one of the five whose 09 §6 row lists an L1 instance at all.
  V-BRK-006 is `L2, L4`; V-BRK-018 and V-BRK-019 are `L2`; V-REV-002 is `L2` and phase 10. The
  property each names is implemented here and asserted by the suite — the write-ahead ordering as an
  exact call sequence, all-or-nothing snapshotting including the two-of-three case the check text
  names, the field-manager string and the dry-run-precedes-apply ordering — but **a property proven
  at a level the check does not list is not that check passing**, and the honest record is an
  implementation with its L2 instance still open. All four go to `broker-execute-l2.sh` in P9-T9.
  This is the same discipline that kept V-GAT-012/022 unclaimed in T3a and the L2 halves of
  V-REV-003/004 unclaimed in T4; writing it down again because the temptation is strongest exactly
  when the evidence is good.
- **P9-T5b** — what happens after the write. `internal/broker/verify/`: the per-kind verification
  predicates of 04 §5.1 with their settle windows, transient-versus-terminal classification, the
  recovery ladder in `status.recovery` with no silently skipped rungs, automatic rollback and the
  cooldown that follows it, rollback-failure paging plus auto-pause, and the selector fan-out
  expanded once against live state before classification. **Claims exactly one check: V-PRO-021 at
  L1.** The row's original "covers V-BRK-014, V-REV-005/006" was wrong in both directions and is
  corrected here rather than inherited. V-REV-005 and V-REV-006 are `L2`, phase 10 — the same shape
  as T5a's four, implemented and asserted here and deliberately not recorded, and both go to
  `broker-execute-l2.sh` in P9-T9. (V-REV-006's level list was later widened to `L1, L2` by
  **T7c-3c-ii-b-1**, which supplied the missing L1 half — a real recorder writing to a real API
  server, which T5b did not have. V-REV-005 is still `L2` and still owed.) **V-BRK-014 is not merely a level mismatch: it is structurally
  unreachable from this unit.** It fault-injects at each of steps 1–10 and asserts the trace shows
  steps 1…k and nothing after; T5b owns step 10 alone, and there is no assembled pipeline to inject
  into until the brake (T6) supplies step 5 and the controller (T7) wires the pair. It is reassigned
  to **T7** below. V-PRO-022 is **deferred on 09 §12 row T-10**, and V-PRO-013 is `L2` and
  additionally blocked on **T-9**; the settle-window numbers this unit had to pick are recorded as a
  decision to be ratified, not as that check passing.

**Two scope boundaries T5a leaves open, stated rather than left to be discovered.** (1) `Executor`
has no caller — `broker.Pipeline` is still `UnavailablePipeline`, because the thing that would
assemble a Request from an envelope needs the brake (T6) and the controller wiring (T7) to exist.
(2) A `patch` against an API that does not honour dry-run is **refused**, not executed. The broker
will not model a server-side merge itself, and modelling it is the only other option: a guessed
merge produces an integrity check that passes on exactly the payload V-BRK-020 exists to catch.

**Two phase-9 pipeline checks are reassigned to T7, found while scoping T5b.** Both are `L1` and
both are in this phase's ratchet, and neither could be claimed by any unit that owns a single step.

- **V-BRK-014** (pipeline step trace) was assigned to T5. It fault-injects at each of steps 1–10 and
  asserts the trace shows steps 1…k and nothing after, with no mutation in the audit log. T5a owns
  step 9 and T5b owns step 10; the property is about the **sequence**, so it needs the whole thing
  assembled. Step 5 is the brake, which is T6, and the thing that constructs a pipeline at all is
  T7's wiring — `broker.Pipeline` is `UnavailablePipeline` until then.
- **V-BRK-011** (pipeline order is observable: classify ≺ gate ≺ snapshot ≺ execute) was in the
  ratchet at the top of this file and **assigned to no task at all** — a planning defect of the kind
  PLAN §3 says to resolve by naming a task rather than discovering it at MILESTONE. Same reasoning,
  same home.

Recording both rather than quietly widening T5b: a unit that claims a check it structurally cannot
exercise is worse than one that leaves it open, because the ratchet then reads as satisfied.

**P9-T6 ships as three units.** The row above is not one deliverable either: it is the brake's
objects, the brake's decision, and the one controller that acts on a brake object. The seam is the
same layering seam T3a/T3b and T5a/T5b used — schema, then the function that reads it, then the
thing that runs. Split before writing code rather than after, per SELECT §2.

- **P9-T6a** — the objects. The rest of `Agent.spec.operations` (06 §1.1's full seven fields),
  `status.operations` and `status.broker`, the three new CRDs (`FleetFreeze`, `ApprovalRoster`,
  `UndoRequest`), their admission webhooks, and `pause` proven inert in the renderer. **Claims
  exactly one check: V-RUN-012 at L0.**
- **P9-T6b** — `internal/broker/brake.go`: the nine fail-closed rules of 06 §4.4 as one decision
  function, plus the contested index. **Claims exactly one check: V-CTR-015 at L1**, allocated in
  09 §6.9 by this unit — see below.
- **P9-T6c** — `internal/controller/undo_controller.go` (`C-UC`), the advisory
  `kube-agents/contested: <action-id>` annotation, and its envtest.

**Seven of T6's eight checks are L2-only, and T6a claims none of them.** The same finding as T5a's
and T5b's, in the same direction. V-BRK-005, V-RUN-007, V-RUN-008, V-RUN-013, V-REV-007, V-GAT-003
and V-GAT-007 are all `L2` in 09 §6: every one of them is about the brake's OBSERVABLE effect on a
running fleet — an agent that stops writing, a freeze that covers a scope, an undo that reverses a
real object — and none is reachable from a Go test, however good. Only **V-RUN-012** lists an L0
instance, and that is what T6a claims. The seven go to `verify-phase9.sh` and `broker-execute-l2.sh`
in **P9-T9**, alongside the ten already routed there.

**T6b allocates V-CTR-015 rather than claiming nothing.** The consequence of the paragraph above is
that T6b — the unit that writes the most safety-critical function in the broker — has no check it
can reach, and would otherwise ship the nine fail-closed rules with their only coverage a shell
script in a later unit that has never been run. The rules are a pure decision function of already-read
inputs, so they are fully exercisable at L1 with no cluster; what was missing was a check ID saying
so. `V-CTR-015` (L1, 06 §4.4, BLOCKING-PHASE) is added to 09 §6.9 and mapped alongside V-CTR-007 on
`03§11#20`, `06§10#45` and `06§10#47`. It does not replace V-CTR-007, which stays L2 and stays T9's:
one asserts the decision function, the other asserts the objects behave that way on a real fleet.
Adding coverage for a property nothing asserted is a tightening, which is the direction PROTOCOL §10
permits; the precedent is P8's `V-CTR-014`.

**The one interpretation in T6b, flagged for a human.** 06 §4.4's pause row says the broker "refuses
new envelopes" and carves out no exception, but **V-REV-007** — "undo works with the originating
agent paused or deleted", BLOCKING-ALWAYS — requires one, because the same section makes an undo a
first-class classified, journaled action, i.e. an envelope through this broker. Resolved by reading
pause the way the same section already reads freeze (`allowUndo` defaults true): **undo is exempt by
origin, not by class.** An undo cannot widen what an agent may newly do, so the exemption preserves
every property pause protects; because an invariant-preserving resolution exists, PROTOCOL §8.5 makes
this a decision and not a halt. The boundary is narrow and tested both ways: undo is exempt from rows
1, 2, 8, pause, and freeze-with-`allowUndo`, and from nothing else — journal, snapshot, undo plan,
roster, budget and post-execution verification all apply to an undo exactly as to any other write.

**T6c allocates V-CTR-016, for the third time and the same reason.** T6c writes `C-UC`, the
controller that actually reverses a change, and every check 09 §6 routes at it is L2: V-REV-007 (L2,
phase 10) and V-REV-001/005/009 all assert an undo against a real fleet. Shipping the preconditions
of an undo with no check that has ever run is the failure mode T6a and T6b already argued; `V-CTR-016`
(L1, 05 §1.3, BLOCKING-PHASE) is added to 09 §6.9 and mapped onto `06§10#41` and `06§10#42`. It
displaces nothing — V-REV-007 stays L2 and stays T9's. Precedents: V-CTR-014 (P8), V-CTR-015 (T6b).
What it asserts is the property a per-branch test would miss: **the preconditions are one shared
predicate and each refuses in isolation against a baseline that is accepted**, plus the two things
that make the linkage trustworthy — that the window is closed AT its boundary, and that a failed
reverse write cannot leave 06 §4.3's bidirectional link one-way.

**Three decisions in T6c, none of them a halt.**

- **The replay route is P9-T7's, not T6c's.** 05 §1.3 step 4 calls
  `POST /v1alpha1/actions/{actionId}/replay`, which does not exist, while V-BRK-021 requires one
  listening port and one mutating route. T6c ships the `Replayer` interface plus `UnavailableReplayer`,
  the same shape `broker.Pipeline`/`UnavailablePipeline` already uses, because the route needs a
  Pipeline to call and T7 is where the pipeline gets constructed. Nothing in T6c claims the route
  exists, and `UnavailableReplayer` makes "not installed" a loud terminal state rather than a silent
  success — `TestUndoWithNoReplayerInstalledDoesNotClaimSuccess` pins it.
- **`undoLinkPending` is a Condition on the `UndoRequest`, not a field on the `ActionRecord`.** 05
  §1.3 names the flag but no API type carries it. It cannot live on the original: the case it exists
  for is precisely a failed write to the original. It is set in the SAME status write that records
  `undoExecuted`, and cleared only once the reverse link lands, so a crash between the two writes
  leaves a durable flag the next reconcile picks up rather than an undo that happened and a record
  that never heard about it.
- **The advisory `contested` annotation is best-effort, and Forbidden is swallowed.** 06 §4.4 says
  the broker "also stamps" it and 05 §1.3 step 5 has `C-UC` mark every target. `C-UC` attempts a raw
  merge patch per target and ignores `Forbidden` and `NotFound`, because the alternative — granting
  the undo controller patch on arbitrary GVKs in every namespace — gives it a write reach larger than
  any agent's, which is the exact shape 03 §3.3 rule 3 exists to prevent. The authoritative refusal
  was never the annotation: it is `status.contested` plus the broker's in-memory index, and 06 §4.4
  says so outright, since the commonest contested case is a human undoing a create and a deleted
  object cannot hold an annotation. Tested both directions.

**P9-T7 ships as seven units**, on the same layering seam T3a/T3b, T5a/T5b and T6a/b/c used: the
thing both halves depend on, then the rendering of the pair, then the objects that pair needs to
actually talk, then the pipeline that runs behind it. (T7d was split out of T7b mid-unit, then split
again into T7d-1/T7d-2, and T7d-2 split once more into T7d-2/T7d-3/T7d-4 when implementing it showed
that its three deliverables live in three different layers with three different verification levels.
**T7d-5 was then added ahead of T7d-4**, from a user question at T7d-3's checkpoint that found the
identities T7d-3 had just written had no install path. See "Why T7b stops at the render", "Why T7d
split in two" and "Why T7d-2 split again" below.)

- **P9-T7a** — `internal/agentlabels/`: the five 08 §2.5 label keys spelled once, and the injective
  scope renderer. Every other T7 deliverable stamps these; nothing else in T7 can be written without
  agreeing on them first. **Claims V-RUN-011 at L0 and L1.**
- **P9-T7b** — the pair itself, as the controller renders it: broker Deployment and
  `<agent>-broker` Service on 8443 applied **before** the agent Deployment, both owner-referenced;
  the pair-atomic `LaunchSpec` and `WorkloadPair`; `BrokerReady`/`AgentReady` with `Ready` their
  conjunction; the `wait-for-broker` init container with observe-and-report on timeout; the five
  `KUBEAGENTS_BROKER_*` env vars, injected last so a CR author cannot redirect them; the actor
  ServiceAccount **name**; goldens. **Claims V-RUN-003 and V-BRK-012, both L0.**
- **P9-T7d-1** — **trust**: the mesh CA (`kubeagents-mesh-selfsign` ClusterIssuer → an `isCA`
  `Certificate` in cert-manager's namespace → the `kubeagents-mesh-ca` ClusterIssuer) as static
  install-time manifests under `config/mesh-ca/`, plus the two per-agent cert-manager
  `Certificate`s behind `<agent>-broker-tls` and `<agent>-mesh-tls`, rendered by the controller and
  owner-referenced. **Claims no new L0 check**; six L1 property tests, of which the load-bearing one
  is the SPIFFE binding (below).
- **P9-T7d-2** — **the pair's own NetworkPolicies**, rendered by the controller and
  owner-referenced: `<agent>-broker-ingress` (the broker default-deny on ingress, admitting exactly
  the peer matching `kube-agents/agent: <name>` **and** `kube-agents/role: reader`) and
  `<agent>-to-broker` (the agent's one egress hop to :8443). **Claims no new L0 check** — six L1
  property tests over the selectors; the packet-level properties are V-ISO-001/002 at L2 in P9-T9.
- **P9-T7d-3** — **the actor identity**: the actor `ServiceAccount` per agent and the Role/RoleBinding
  carrying **exactly** 06 §2.2.1's broker-operations grant and nothing else, as GitOps artifacts under
  `policy/rbac-overlay/` with the derived exemplars under `examples/gitops-repo/`. **Claims V-BRK-013
  at L0** — the two-sided assertion planning defect 1 resolves Accept (e) into.
- **P9-T7d-4** — **the install-path egress holes**: the API-server rule the broker's own pipeline
  needs, added to `netpol-agent-egress.yaml.template` as a rendered optional block with the
  control-plane CIDR supplied by `vars.sh`, plus the regenerated exemplars. **Done 2026-07-28.**
  Verified by `dev/tests/reference-render.py` at L0 (**V-CTN-020**, L0 half) and by V-ISO at L2 in
  P9-T9.

  **Rule 9 is the one destination in this allowlist that cannot be pinned in a committed file**, and
  that shaped the whole unit. Every other address here is published and stable — Google's restricted
  VIP, GitHub's four blocks, GKE's two metadata pairings — so the exemplars can state them as facts.
  The API server's is per-cluster, and these clusters are public GKE (no `--master-ipv4-cidr` or
  `enable-private-nodes` anywhere under `k8s-operator/scripts/` or `dev/cluster/`), so the endpoint
  is a bare IP with no range anyone publishes. A committed exemplar could only pin a fiction about
  somebody's cluster. So `provision_13` resolves it at apply time — the `kubernetes` Service
  ClusterIP, the kubeconfig endpoint, or an explicit `KUBE_APISERVER_CIDR` — and **refuses to apply
  without one** unless `KUBE_APISERVER_EGRESS_ENABLED=false` is set deliberately.

  **That inverts this file's default on purpose.** Every other optional block is absent-unless-asked
  because absent is the safe direction. Rule 9's absent direction closes the broker's write path:
  no TokenReview (pipeline step 1), no FleetFreeze read (step 5), no ActionRecord write (step 11),
  and no kubectl-shaped skill for the reader — reported to the operator as an authentication error
  that never mentions the network. A default of "absent" would rebuild the hole this unit closes.

  **Two changes beyond the literal scope of the bullet.** First, `dev/tests/reference-render.py`
  gained a **source** property (10) as well as three behavioural ones: properties 7–9 are all true
  of a resolver nobody obeys, so an edit turning `provision_13`'s `else` arm into a warning would
  leave them green. Second — and this is the real find — the byte-for-byte gate caught the drift in
  the exemplars (regenerated; comment-only, no rule 9 in them) and `dev/test_skill_templates.py`
  then caught the third copy, which surfaced that the `propose-developer-team` bundle is a **second
  install path** for the developer-team tier: it is applied by the customer's CI/CD, not by
  `provision_13`, so a tenant provisioned through the F4 cascade would have shipped without rule 9.
  Hence `--kube-apiserver-cidrs` on `render_developer_team.py`, documented **unbracketed** in
  SKILL.md as the one flag whose omission is not the conservative choice, and bound in both
  `WIDE_ENV` and `WIDE_FLAGS` so the two halves cannot diverge.

  **Deliberately not in scope**: the L2 half of V-CTN-020 — that the policy is actually enforced and
  that the broker's pipeline actually completes over it — is P9-T9's, and P4 still governs it (on a
  non-enforcing dataplane an egress claim is `deferred`, never `pass`). Nor does this resolve a
  hostname in the kubeconfig `server:` URL: a policy pinned to whatever DNS answered at install time
  stops matching after a control-plane rotation, silently, and refusing is the better failure.

- **P9-T7d-5** — **the install path for the identities T7d-3 just wrote.** Added 2026-07-28, run
  before T7d-4, **done 2026-07-28**. Renders the reader and actor `ServiceAccount`s, the shared
  broker-operations `ClusterRole`/`Role` and the two bindings from `common.sh` — the
  `render_tenant_quota` / `render_wi_metadata_block` idiom, one source and one render — applied from
  numbered steps in `provision_08` (platform) and `provision_12` (cluster-admin, developer-team),
  with matching `delete_agent_identity` calls in both teardowns. **Claims V-CMP-007 at L0** as
  `dev/tests/identity-has-install-path.py`.

  **Why it exists.** T7d-3 shipped the actor identity into `policy/rbac-overlay/` and the per-cluster
  bundles, and a user question at CHECKPOINT surfaced that nothing on the install path creates it.
  The accurate finding, narrowed after a read-only sweep of the live `platform-agent-host`, has three
  parts: the cluster-admin and developer-team **reader** SAs _were_ created imperatively, inline in
  `cluster-admin-agent.yaml.template` and `developer-team-agent.yaml.template`; the **platform**
  reader SA was created by nothing at all — `kubeagents-platform-agent` on the live cluster is a bare
  hand-applied SA whose `last-applied-configuration` carries no labels and whose Workload Identity
  annotation is not in it either; and **no actor SA and no broker-operations grant existed anywhere
  on any install path**, so the broker Deployment T7d-3 renders would reference an identity that does
  not exist and the pod would not start. No install-path identity carried `kube-agents/role`, which
  is the label both VAP arms now select on.

  That is [[LSN-039]], an escape against the already-closed [[LSN-007]]: `install-path-wired.py`
  walks the _script_ graph and every one of its five properties passes on a repository whose steps
  run perfectly and apply none of the security manifests. `common.sh:656` had already found and fixed
  the same class for the tenant quota and the namespace default-deny without generalizing, so this is
  the third instance.

  **Two changes beyond the literal scope, both single-definition-site moves.** The inline
  `ServiceAccount` blocks were **deleted** from the two tier templates rather than relabelled, so the
  reader identity has exactly one source; what stays in those templates is the tier's authority.
  And `platform-agent.yaml.template` gained `spec.scope.projectId`: without it the platform actor
  renders `platform--actor` and the broker's `validate()` refuses an empty `--scope`, so the pair
  would come up `BrokerReady=false` forever. `spec.scope` is mutable (only `spec.tier` is immutable),
  and the added scope still strictly contains every cluster-admin scope under it.

  **Deliberately not in scope**, by user decision: the 45 stale `app.kubernetes.io/managed-by: gitops`
  sites across 24 files, and the 08 §2 / §2.7 / §4 "GitOps-managed" wording that contradicts 05 §C13
  and 06 §4. Those are documentation and a spec correction; this is a pod that will not start.
  **Also not in scope, and carried forward explicitly**: the planned declare-or-fail table over
  `examples/gitops-repo/` — V-CMP-007 walks the manifest→step edge for the **install path**
  (`k8s-operator/scripts/`), which is what makes the identities real, but it says nothing about which
  files in the exemplar tree are inert. That property belongs to the queued sweep unit, which is the
  unit that touches that tree.

- **P9-T7d-6** — **make the install overlay render, and render faithfully.** Added 2026-07-28, run
  before T7c-3c-ii-b-2-b, **done 2026-07-28**. Pins six ambiguous `Certificate` replacement
  selectors to `name: serving-cert`; lifts `../mesh-ca` out of `config/default` into a new
  transformer-free `config/install`; repoints `deploy`, `undeploy`, GitOps bootstrap wave 10 and the
  `propose-cluster-admin` template at `config/install`; adds a `render` target wired into `build` and
  `test`. **Claims V-CMP-008 at L0** as `dev/tests/install-render-is-faithful.py`.

  **Why it exists, and why it is not part of 2-b.** Surveying for T7c-3c-ii-b-2-b — which gives C-BR
  its own ServiceAccount, RBAC and Deployment, and therefore has to render and apply them — found
  that `kustomize build config/default` exits non-zero and has done since PR #44 (`1385649`,
  2026-06-28) landed the mesh CA. `make deploy` is the sanctioned install path and
  `provision_03_gcp_gke_operator.sh` goes through it, so for a month the install did not work at all
  and nothing said so: no L0 line, no L2 line, and no CI workflow renders the overlay. 2-b is
  unachievable on top of it, so this is sequenced ahead, on the same precedent as T7d-5.

  **Two defects, one root cause, and they must ship together.** The visible one is the render error:
  the mesh CA added a second and third `Certificate`, which made two `replacements` selectors written
  as bare `kind: Certificate` match three objects. The one it was hiding is worse. `config/default`
  carries `namePrefix: kubeagents-` and `namespace: kubeagents-system`, a kustomize transformer
  reaches every resource beneath it with no per-resource opt-out, and the CA cannot survive either:
  the prefix renames `ClusterIssuer/kubeagents-mesh-ca` — the one string `meshCAIssuerName` in
  `mesh_trust.go` hardcodes — into `kubeagents-kubeagents-mesh-ca`, and the namespace moves the CA
  `Certificate` out of `cert-manager`, which is the only namespace a `ClusterIssuer` resolves
  `ca.secretName` from. Neither rewrite errors and both apply. **Pinning the selectors alone is
  strictly worse than the status quo**: today nothing installs; with only the pin, a broken trust
  root installs silently and surfaces days later as brokers that never become Ready behind agent
  `Certificate`s stuck `Pending`. That coupling is why this is one unit and not two.

  **Beyond the local fix, because the local fix does not reach a real cluster.** GitOps bootstrap
  wave 10 and the `propose-cluster-admin` skill's `10-controller` template both pull the overlay by
  URL, both were pinned to `config/default`, and that is the path a cluster actually takes. Left
  alone they would bootstrap a control plane with no trust root even after `make deploy` was correct.

  **The mechanization is deliberately split across two levels**, because "it renders" and "what it
  renders is the install" are different properties with different costs. The first is `make render`,
  a prerequisite of `build` and `test` — it needs the kustomize binary, so it cannot be an L0 line
  (`.github/workflows/l0-checks.yml` installs no dependencies on purpose; a check that needs a
  package is not L0), and CI reaches it because `k8s-operator-test.yml` runs `make -C k8s-operator
test`. The second is the L0 check, which asserts the **reference graph** rather than the output:
  no transforming kustomization may reach `config/mesh-ca` over the whole inclusion graph, not just
  the edge that broke, so re-nesting the CA under a new transforming layer next year fails too.

**Why T7d split in two.** Two reasons, and the second one changed what T7d-2 is allowed to contain.

The first is the level seam, which is the same one that produced T7b/T7d: T7d-1's properties are
_renderable_ — one issuer for both ends, the right SPIFFE URI, key rotation on renewal, the Secret
names matching what T7b mounts — and every one of them is an L1 assertion that would otherwise
surface at L2 as a TLS handshake error, the least informative available report. T7d-2's properties
are packet-level (V-ISO-001/002 assert a packet is _dropped_) and are already routed to P9-T9.

The second is that **the actor ServiceAccounts cannot be controller-minted**, and finding that out
is what forced the split rather than merely justifying it. P1-T4/T5 (08 §4) already settled this for
the reader identity: the controller holds `serviceaccounts: get;list;watch` and a comment in
`agent_controller.go` reading _"Do not re-add RBAC write verbs"_, because agent identity is
pre-created and GitOps-managed, enforced by `vap-agent-readonly`. The actor SA is the _higher_-
authority half of the pair, so if the reader's may not be minted at runtime, the actor's certainly
may not — 06 §2.2.1's "the ability to name the actor identity is the ability to choose an authority
level" applies with more force, not less. The actor identity is therefore a GitOps-artifacts unit
(`policy/rbac-overlay/`, `examples/gitops-repo/`), not a controller unit, which is a different kind
of work from T7d-1 and shares no code with it.

**Why T7d-2 split again.** The sentence above lumped three deliverables under "GitOps artifacts", and
implementing the first one showed that only two of the three are. **The pair's NetworkPolicies are
controller output, not install-time YAML** — 08 §2.7's grant table gives the controller full CRUD on
`NetworkPolicies`, bounded to "objects the controller owns via `OwnerReference`", and
[05](../design/05-system-architecture.md) §1 C1 lists "the pair's NetworkPolicies" among what the
controller reconciles. They cannot be install-time artifacts anyway: they select on
`kube-agents/agent`, and the CR that value names does not exist when the installer runs. The per-tier
egress policies stay exactly where they are, because they select on `kube-agents/tier` and encode a
fleet decision a human makes in a PR. So the split is by **layer**, and it lines up with the levels:
selectors are L1, RBAC verbs are a static L0 assertion (V-BRK-013), and the install-path template has
its own L0 check in `reference-render.py`.

**What T7d-2 found and did not close: the broker cannot reach the API server.** The broker pod carries
`kube-agents/tier`, so the per-tier egress policy selects it and makes it default-deny on egress — and
that allowlist has no API-server rule. Its four destinations are DNS, the control namespace on
:80/:8080 (LiteLLM and the token minter), `restricted.googleapis.com`, and GitHub's published CIDRs.
None of those is the kube-apiserver, which the broker needs for **three of its eleven pipeline steps**:
TokenReview (step 1), the FleetFreeze read (step 5), and the ActionRecord write (step 11). Nothing
rendered by the controller can fix it — NetworkPolicy cannot name a Service, so "allow the
`kubernetes` endpoint" is not expressible, and the control-plane CIDR is per-cluster and known only at
install time. It is **P9-T7d-4**, and it is called out here rather than left in a code comment because
the symptom is a broker that authenticates nobody, which reads as an auth bug and sends the debugger
at `internal/broker/auth.go`. Note that the same gap has been latent for the READER since Phase 5 —
whether the agent's own API reads survive it is an L2 question, and P9-T9 is where it gets asked.

**Closed by T7d-4 as egress rule 9** (2026-07-28). The prediction above held in every respect except
one, and the exception is worth keeping: "the control-plane CIDR is per-cluster and known only at
install time" is right, but it understates the case for a public GKE endpoint, where there is no
_range_ at all — only a bare IP that changes when the control plane rotates. That is why rule 9 is
the one rule in the file with no committed exemplar and why `provision_13` refuses to apply without
resolving it, rather than shipping a default. It also emits **both** address forms (the `kubernetes`
Service ClusterIP and the kubeconfig endpoint), because whether NetworkPolicy sees egress before or
after DNAT is dataplane-specific — the same reason `GKE_DATAPLANE` defaults to `auto`.

**What T7d-1 found: the mesh certificate is half of the broker's identity check, not just its
transport.** `internal/broker/auth.go` authenticates the caller by TokenReview, compares the result
against the single `ExpectedCaller` it serves, and then _binds the two layers_ — it refuses with
`ReasonPeerMismatch` unless the client certificate's SPIFFE URI equals the ID derived from the
token. So `<agent>-mesh-tls` must carry `spiffe://cluster.local/ns/<ns>/sa/<readerSA>` exactly, or
every envelope in the fleet is refused at the transport layer, with an error message about trust
domains, discoverable only at L2 after a rollout. The format now has **one definition site**,
`broker.SPIFFEID`, called by both `auth.go` and the renderer; a test asserts both that the rendered
URI equals what that function produces _and_ that the function still produces the canonical
`spiffe://<td>/ns/<ns>/sa/<sa>` shape, since two callers agreeing with each other is not the same as
being right.

**And a uniqueness dependency worth stating.** Certificate and Secret names derive from `agent.Name`
and are unique because the API server says so; the _actor_ SPIFFE ID derives from `(tier, scope)`
and not from the name at all (06 §5.1 forbids the name being an input, since naming the identity is
choosing the authority). Its uniqueness therefore rests entirely on admission enforcing (tier, scope)
uniqueness fleet-wide. Two same-tier same-scope agents would get distinct certificates carrying the
_same_ actor identity — unrepresentable today, but the mesh's identity uniqueness is a property of
the **webhook**, not of the renderer, and nothing in the renderer would notice if that changed. This
was found by writing the collision test with a two-agent fixture and having it fail (LSN-015 again:
one CR could not have caught it).

**cert-manager's Go types are deliberately not a dependency.** Adding
`github.com/cert-manager/cert-manager/pkg/apis/certmanager/v1` was tried and reverted: it upgrades
every `k8s.io/*` module in the operator and pulls `sigs.k8s.io/gateway-api` — an unrelated API
surface — into the binary that reconciles the write-credential path. Two struct literals do not
justify that, and the controller only ever _writes_ these objects, so the type safety traded away is
type safety over a value nothing in this process reads. They are rendered as `unstructured`.

- **P9-T7c** — the pipeline behind the pair: **V-BRK-011** and **V-BRK-014** at L1, the
  `ChangePolicy` informer T3b deferred here (V-GAT-009's L2 instance stays open), and the
  `POST /v1alpha1/actions/{actionId}/replay` route plus the HTTP `Replayer` T6c deferred here.
  **Split into four**, see "Why T7c split into four" below.
  - **P9-T7c-1** — `internal/broker/steps.go` and `internal/broker/pipeline/`: the observable step
    trace and the assembly of steps 3–11. **Claims V-BRK-011 and V-BRK-014 at L1. Done
    2026-07-28.**
  - **P9-T7c-2** — the two deferrals, **split again into 2a and 2b** when 2b turned out to be a
    halt. See "Why T7c-2 split" below.
    - **P9-T7c-2a** — the live `ChangePolicy` source (from T3b): `internal/broker/policy/` and the
      `pipeline.Config.Classifier` seam. **Re-records V-GAT-009 at L1 over the live loader. Done
      2026-07-28.** V-GAT-009's L2 instance stays open.
    - **P9-T7c-2b** — the `POST /v1alpha1/actions/{actionId}/replay` route plus the HTTP
      `Replayer` (from T6c). **DEFERRED out of Phase 9, 2026-07-29, by human ruling** on the halt
      recorded below. Blocker: **a human decision on which of 05 §1.3 and 03 §4.1 is authoritative
      about the `/replay` route** — the two specs disagree and PROTOCOL §8.5 forbids the harness
      picking a side. Nothing else in Phase 9 depends on the route, so the phase closes without it
      and it is rescheduled to the phase that resolves the spec.

      **V-BRK-021 is _not_ deferred and stays green.** It is BLOCKING-ALWAYS, and a BLOCKING-ALWAYS
      check may never be deferred. What is deferred is the _task_; the check continues to assert what
      it asserts over the routes that exist. This distinction is the whole reason the ruling was
      safe to take. **The blocker CLOSED 2026-07-30** — 09 §6 was edited by T7c-2c
      below, which is exactly the promotion condition the deferral row named. The task itself moves
      to Phase 10, where it is one unit with `/approve` against the one reshaped check.

    - **P9-T7c-2c** — **done 2026-07-30. The ruling arrived, and it is option (a): reshape
      V-BRK-021.** Re-records **V-BRK-021** at L0 over the new form; sweep 10/10. Scheduled
      2026-07-30 from `BACKLOG.md` **B-003**, a human ruling on the deferral row 2b opened. The row's
      promotion condition was one sentence — _"this row closes when 09 or 05 is edited"_ — so this
      task is what closes it. **`todo`, and it is the next unit**, ahead of T8b-4b-ii-2b and T9b,
      because it is L0 and this phase's own ordering rule puts the remaining L0 work in front of the
      remaining L2 work. Three things, all small: rewrite 09 §6's V-BRK-021 row so the assertion is
      **an equality against the registered handler set** rather than a count; make
      `Server.MutatingRoutes()` derived from, or cross-checked against, that registered set instead
      of the hand-written `[]string{ActionsPath}` at `server.go:177`; and stop
      `server_test.go:433`'s `strings.Count(src, "s.mux.HandleFunc(") != 4` being the thing the
      property rests on. **What it is not:** it does not implement `/replay` or `/approve`. Those
      stay in Phase 10 beside P10-T4 / P10-T7, where the item asks for them to be one unit against
      one reshaped check. It also does not settle V-BRK-021's L0-vs-L2 evidence gap — that is T9b's.
      **Why this is not a PROTOCOL §10.2 halt** even though the new form admits three mutating routes
      where the old admitted one: §10.2's remedy is a halt _for human review_, and the review has
      already happened — the deferral row named exactly two admissible rulings and a human picked (a)
      by name. The argument is recorded in full in `BACKLOG.md` §Scheduled under B-003.
  - **P9-T7c-3** — **the runtime wiring.** Real client-backed adapters for the twelve seams
    `pipeline.Config` takes — `LiveState`, `Applier`, `Reader`, `BodyStore`, `Prober`,
    `Rollbacker`, `Pager`, `Pauser`, the cooldown registry, `ActionHistory`, `ReferenceIndex`,
    `BrakeSource` — and a `pipeline.New` call in `cmd/broker/main.go` where
    `broker.UnavailablePipeline{}` is today. Until it lands, **LSN-007 applies to the whole
    pipeline**: it is built, tested, and unreachable from the binary. **Split into four**, see "Why
    T7c-3 split into four" below.
    - **P9-T7c-3a** — `livestate.Source`, the `LiveState` adapter: the five reads every
      classification rung depends on. **Claims V-GAT-022 at L2. Done 2026-07-28.**
    - **P9-T7c-3b** — `undo.ReferenceIndex` and `execute.BodyStore`. **Allocates and claims
      V-REV-010 at L1 and L2. Done 2026-07-28.**
    - **P9-T7c-3c** — the verify adapters: `verify.Prober` (eight methods), `Rollbacker`, `Pager`,
      `Pauser`, `CooldownRegistry`. **Split into three at ORIENT**, see "Why T7c-3c split into
      three" below.
      - **P9-T7c-3c-i** — `internal/broker/probe`, the `verify.Prober`: the eight probes behind the
        eight rows of the 04 §5.1 table. **Allocates and claims V-PRO-027 at L1 and L2. Done
        2026-07-28** — seven of the eight rows exercised; the eighth, connectivity, is deferred with
        a named human owner, because a prober that can dial from another pod's network position is a
        deployable workload with its own RBAC and blast-radius argument, not a method body.
      - **P9-T7c-3c-ii** — `Rollbacker`, `Pager`, `Pauser`: the three effects rungs 3 and 5 of the
        04 §5 ladder actually have on the world. **Split into two at IMPLEMENT**, see "Why
        T7c-3c-ii split into two" below.
        - **P9-T7c-3c-ii-a** — `internal/broker/rollback`, the `verify.Rollbacker`: rung 3, the
          replay itself. **Allocates and claims V-REV-011 at L1 and L2. Done 2026-07-28.**
        - **P9-T7c-3c-ii-b** — `Pager` and `Pauser`, rung 5, **plus the controller-side C-BR
          reconciler they both have to go through.** Blocked on a component that does not exist,
          which is why they are not in ii-a. **Split into two at IMPLEMENT**, see "Why T7c-3c-ii-b
          split into two" below.
          - **P9-T7c-3c-ii-b-1** — the **request** side: `status.escalation` on the `ActionRecord`,
            `internal/broker/escalate` behind `verify.Pager` and `verify.Pauser`, and the
            `Pauser` interface change that lets a pause name the record it belongs to. **Claims
            V-REV-006 at L1**, whose level list is widened from `L2` to `L1, L2` — a strengthening,
            nothing removed.
          - **P9-T7c-3c-ii-b-2** — the **fan-out** side: the controller-side C-BR reconciler that
            turns a recorded escalation into `spec.operations.paused` and a page. **Claims V-REV-006
            at L2**, which needs the operator image rolled by digest — **P1 in full**, for the first
            time in this task chain. **Split again at IMPLEMENT** under the `harness-run` §2 sizing
            rule, because the deploy half is a different kind of work from the code half and
            carrying an oversized unit forward is what the rule forbids:
            - **P9-T7c-3c-ii-b-2-a** — the reconciler, its two rows in
              `vap-agent-scope-journal`, and the L1 suites. No deploy, no cluster, no new identity;
              the controller is deliberately wired into no manager yet. **Claims V-REV-006 at L1**
              — the fan-out half, completing the L1 story ii-b-1 opened. **Done 2026-07-28.**
            - **P9-T7c-3c-ii-b-2-b** — C-BR's own ServiceAccount, RBAC, Deployment and kustomize
              wiring, the `--controllers` selector in the manager binary, `make cloud-build-push`,
              a roll by digest, and **V-REV-006 at L2 with P1 in full**. **Done 2026-07-29** — 39
              assertions, exit 0, twice. The `ClusterRole` is hand-written and carries no
              `+kubebuilder:rbac` markers, because a marker on `BrakeReconciler` composes into the
              operator's `manager-role`; the `Deployment` lives in `config/manager` and not a base
              of its own, because the `images:` transformer reaches only what is beneath the
              kustomization declaring it; and `parseControllers` refuses to combine `brake` with
              any other controller, because a process runs as one ServiceAccount and 06 §4.3 keeps
              C-BR's and the exporter's authority over `ActionRecord.status` disjoint. Opened
              [[LSN-044]] and [[LSN-045]], both defects in the check this unit authored.
      - **P9-T7c-3c-iii** — a durable `CooldownRegistry`. `verify.MemoryCooldown` says in its own
        doc comment that it is "deliberately not the production store: a cooldown that dies with
        the broker process is a cooldown an operator can clear by deleting a pod, and 04 §4.2
        controls must survive that." **Allocates and claims V-PRO-028 at L1. Done 2026-07-29** —
        `internal/broker/cooldown`, derived from the `ActionRecord` journal, sharing the backoff
        fold (`verify.CooldownSeries`) with the reference implementation so the two agreeing is a
        property a test asserts rather than two transcriptions somebody keeps in step. See "What
        T7c-3c-iii asserts" below.
    - **P9-T7c-3d** — `pipeline.BrakeSource`, `broker.ContestedIndex`, and the `pipeline.New` call
      in `cmd/broker/main.go` that replaces `broker.UnavailablePipeline{}`, plus `policy.Source`
      construction with a synchronous startup `Refresh`. **Closes LSN-007.** Necessarily last: it
      is the only sub-unit that needs every other adapter to exist. **Split four ways at ORIENT**
      under the `harness-run` §2 sizing rule. The task text assumed the wiring was the work and
      that "every other adapter" already existed. It does not: reading the seams found **three
      production implementations missing outright**, two of which the pipeline refuses to start
      without and one of which no code has ever constructed. Wiring `pipeline.New` on top of them
      would either not compile or would compile into a broker that fails every non-dry-run
      execution at the write-ahead check — so the wiring is genuinely last, and there are three
      units in front of it rather than none.
      - **P9-T7c-3d-i** — `internal/broker/brake`, the production `pipeline.BrakeSource`. Gathers
        the four inputs 06 §4.4 needs that are reads: the broker's own `Agent` CR (row 2), the
        cluster-scoped `FleetFreeze` list stamped with `ObservedAt` (row 1), the resolved
        `ApprovalRoster` (row 6), and journal reachability (row 3). `Observe` returns no error by
        the interface's own design — an observer that could not read says so IN the view.
        **Allocates and claims V-CTR-017 at L1. Done 2026-07-29** — `internal/broker/brake`, direct
        reads on a 5s TTL, where `FreezeView.ObservedAt` is the instant of the **read** so the cache
        degrades into row 1 on `Decide`'s own arithmetic with no liveness tracking in the source.
        `refresh` attempts every read even after an earlier one fails, so the reported row is the
        one whose input actually failed. 20/20 mutations caught through `dev/mutate.sh` — **14/20
        on the first pass**; see "What T7c-3d-i asserts" below for the two real survivors, both of
        which were [[LSN-035]] in miniature.
      - **P9-T7c-3d-ii** — the 04 §4.2 budget and flap accountant, which fills the fifth input,
        `BrakeBudget` (row 7). Split out because it is not a read: it is journal-derived
        accounting over windows and thresholds, the same shape and size as the `cooldown` source
        T7c-3c-iii spent a whole unit on. **Split again, at ORIENT for ii, under the
        `harness-run` §2 sizing rule** — the seam turned out to be wrong, not merely unfilled, and
        fixing a cross-package seam plus writing a `cooldown`-sized package plus adding an API
        defaults helper is three units in a coat.
        - **P9-T7c-3d-ii-a** ✅ **done 2026-07-29** — **the seam.** `broker.Accountant` +
          `BudgetQuery{Agent, Trigger, Class, Targets, Now}`, queried from `pipeline.Config` at
          decision time. T7c-3d-i had put the accountant on `brake.SourceConfig`, reachable only
          through `pipeline.BrakeSource.Observe` — a **per-agent** observation taken **before**
          classification — but 04 §4.2 budgets an agent's `{origin, class}` bucket and flaps per
          target, so that accountant could never answer the question the spec poses. Row 8's
          `ContestedIndex` already had the right shape. `BrakeView.Budget`, `brake.Accountant`,
          `brake.Unaccounted` and `brake.SourceConfig.Accountant` are **deleted**. A nil accountant
          now refuses and escalates; a zero `BrakeBudget` still permits, and those two stopped
          being the same value. **V-CTR-018**, L1.
        - **P9-T7c-3d-ii-b** ✅ **done 2026-07-29** — **the accountant.**
          `internal/broker/budget.Source`: the journal-derived fold, `EffectiveInitiativeBudget()`
          over the 06 §1.1 defaults, and the origin partition. **V-PRO-029**, L1, newly allocated in
          09 §6.6 — the true sibling of V-PRO-028 (same suite, same source, same level, same phase,
          same journal-derived-and-refuses-when-blind argument). Row 7 now counts.

          **Recon 2026-07-29 — what ii-b must settle at PLAN, before any code.** The mechanical
          model is **`policy.Source`, not `cooldown.Source`**: cooldown refreshes lazily from inside
          a ctx-taking method, which `Accountant.Budget(q) BrakeBudget` cannot do. Copy cooldown's
          _derivation and test structure_; copy policy's `Refresh(ctx) error` + `Run(ctx)` ticker +
          ctx-free `Current()` _lifecycle_. Six things the spec does not settle, each to be recorded
          as a decision or escalated:
          - **The window model is contradictory and it changes the refusal.** 04 §4.2 and 06 §1.1
            say "rolling"; but `status.budget` carries `windowStart`/`dayWindowStart` with a
            clock-aligned example, and 06 requires `retryAfterSeconds` "to the next **window
            boundary**", which a sliding window does not have. If no reading preserves both, that is
            PROTOCOL §8.5 and a halt — do not pick a side quietly.
          - **Flap's `(target, intent)` key is unimplementable as written.** `spec.intent` is
            free-text model prose, and `internal/broker/idempotency.go` **deliberately excludes** it
            from the idempotency key for exactly this reason ("a retry that reworded itself would
            compute a different key"). Keyed literally on intent, flap under-fires against an LLM
            that rewords. No canonical intent identity exists anywhere in the tree.
          - `> N` vs `>= N` is undetermined (04 says "more than _N_"; 06 says "repeats", default 3),
            and **oscillation has no threshold or window at all** despite V-PRO-016 asserting it.
          - **The 06 §1.1 defaults exist in no Go file** — only the _ceilings_ do, in
            `internal/webhook/agent_webhook.go`. `EffectiveInitiativeBudget()` introduces the default
            table to Go for the first time; put defaults **and** ceilings in `api/v1alpha1` and have
            the webhook import them, or the two copies drift. Follow `ApprovalRoster.EffectiveTTL`,
            which already documents the right asymmetry: admission **rejects** an over-ceiling leaf,
            the runtime **clamps** one that got in anyway.
          - **A cold accountant must report `Exhausted: true`.** `Budget` has no error channel and a
            zero `BrakeBudget` permits, so "I have not read the journal yet" must be encoded as a
            refusal with a distinguishable `Detail` — otherwise every broker restart silently
            disables row 7, which is the hole ii-a just closed. **That clause is the heart of
            V-PRO-029** and has no V-PRO-028 analogue.
          - **Out of scope but must not be assumed done:** `AgentStatus` has no `budget` field and
            the broker has **no write verb on `agents`** (V-BRK-013, BLOCKING-ALWAYS), so 06's "names
            the empty bucket in `status.budget.exhaustedBuckets`" needs a controller, not the broker.
            Likewise `retryAfterSeconds` is currently the flat `PausedRetryAfterSeconds` (60), and
            `BrakeBudget` has no field an accountant could use to supply the real one.

          The journal _can_ answer the partition — `kube-agents/trigger` × `kube-agents/risk-class`
          are both labels — but there is **no agent-name label** (only tier and a non-injective scope
          leaf, so filter client-side on `Spec.AgentRef`) and **no time index** (filter client-side,
          exactly as `cooldown.derive` already does).

          **How the six were settled at PLAN, 2026-07-29 — no halt.** Each is a decision in the
          ledger; the argument lives beside the code it governs.

          1. **The window is rolling, and the two sentences do not contradict.** A sliding window
             _does_ have a next boundary: the instant its **oldest counted charge ages out**, which
             is exactly when capacity returns. That reading satisfies "rolling" _and_
             `retryAfterSeconds` to "the next window boundary", so this is not PROTOCOL §8.5.
             "Rolling" is normative three times across two documents; the clock-aligned reading
             appears once, in a YAML comment on a status field that has no writer. Direction matters
             too — a tumbling hour lets an agent spend a full allowance at 16:59 and another at
             17:01. Recorded at `budget.Window`; the boundary is computed by `snapshot.retryAt`.
          2. **Flap keys on the target alone**, which is **strictly stricter** than
             `(target, intent)`: every literal breach is also a target breach, so nothing the spec
             would catch is missed. Keying on prose would under-fire against an LLM that rewords,
             which is precisely why `idempotency.go` excludes intent. The residual runs the other
             way and is named rather than implied: three legitimately-different actions on one object
             inside the window now trip a brake the literal spec would not. Tolerable because 04
             §4.2's own remedy is "stop, mark, escalate" — a human looks — and both threshold and
             window are operator-tunable. Recorded at `budget.flapKey`.
          3. **`applied = prior + 1`, breach iff `applied > threshold`** — 04 §4.2's "more than _N_
             times" counts the action being decided. With the default 3, three priors are allowed and
             the fourth is refused.
          4. **Oscillation is out of scope.** V-PRO-016 is **phase 13, L2/L4** — it needs a live
             fleet, not a fold. Nothing in Phase 9 binds it.
          5. **`api/v1alpha1/budget.go` is the one Go definition site** for the whole 06 §1.1 table,
             defaults _and_ ceilings; `internal/webhook/agent_webhook.go` now imports the ceilings
             from it instead of transcribing them. `EffectiveInitiativeBudget` follows
             `ApprovalRoster.EffectiveTTL`'s asymmetry — admission rejects, runtime clamps — with one
             deliberate divergence: an **explicit `0` is honoured**, because a zero allowance is a
             real configuration and a zero TTL is not.
          6. **A cold or stale source returns `Exhausted: true`** with a `Detail` naming the
             blindness, distinct from the "you spent it" refusals. As predicted, this is the heart of
             V-PRO-029.

          **Two consequences worth stating rather than discovering.** Charging follows 06 §1.1
          exactly: `Rejected`, `forbidden` and dry-run charge nothing; `RolledBack` charges because it
          ran; `PendingApproval` and `Expired` charge because `gatedPerHour` counts **submissions**;
          `undo` is exempt from every hourly bucket and is never refused for budget, but flap still
          applies to it. And because `applied` excludes dry runs while the whole of Phase 9 is
          dry-run, **the flap brake cannot fire until T7c-3d-iv wires execution.** That is correct — a
          rehearsal did not touch the object — and `TestFlapCannotFireDuringPhaseNine` will start
          failing on the day it stops being true.
      - **P9-T7c-3d-iii** — the two small journal-derived adapters the pipeline needs and nobody
        wrote: `execute.Journal` (`ConfirmDurable`, three test stubs and no implementation — with
        it nil, `Executor.Journal` is nil and **every non-dry-run execution fails the write-ahead
        check**) and `classify.ActionHistory` (the novel-action question; `policy.SourceConfig`
        takes one and no production value exists). **Split at SELECT on 2026-07-29** under
        `harness-run` §2 sizing: the two adapters share only the phrase "journal-derived". One is a
        confirmation on the write path with its own envtest harness; the other is a lifecycle
        question about refresh and staleness on the classify path. Sized together they were one
        unit with two PLANs.
        - **P9-T7c-3d-iii-a — the write-ahead confirmer** ✅ (2026-07-29)
          New package `internal/broker/writeahead`: `Confirmer` is the production `execute.Journal`.
          It lives in its own package rather than in `internal/journal` because
          `internal/broker/execute` already imports `internal/journal`, so a journal-side adapter
          could not hold the `var _ execute.Journal` assertion without a cycle — the
          `internal/broker/bodystore` precedent, followed deliberately.
          **Check: V-BRK-028** (new, L1). The gap to **V-BRK-022** is not an error: that ID is
          reserved above by T7c-4 and IDs are never renumbered (09 §4).
          **What the check is actually about.** `ConfirmDurable` receives only `(ctx, actionID)`, so
          it cannot compare the stored record against caller intent. What it can check is the thing
          an in-process buffer cannot fake: **server-assigned `uid` and `resourceVersion`**. That is
          [[LSN-034]] applied to durability — a store that reported its own success would be
          comparing a value against itself. Four more arms follow from the same argument: a record
          on its way out (`deletionTimestamp`) is not durable; a record whose `spec.actionId`
          disagrees with the name it was derived from is somebody else's journal entry; an
          unreadable journal is refused rather than scored durable; and a misconfigured confirmer
          refuses **before reading**, the same direction as the nil accountant in ii-a.
          **The phase arm, and the measurement under it.** A record whose status label names a phase
          other than `Executing` is refused. It reads the **metadata label**, not `status.phase`,
          and the envtest half proves why: `ActionRecord` carries a status subresource, so
          `client.Create` drops `status` entirely, while `journal.Labels` reads the caller's phase at
          Create time and writes it into metadata, which survives. Reading `status.phase` here would
          have looked more correct and would have been vacuous — it is empty for every record this
          function will ever see.
          **The future the phase arm is for, recorded rather than hand-waved.**
          `journal.Store.Create` folds `AlreadyExists` into a nil return — correctly, since the
          record name is derived from the action id, which is what makes the broker's retry safe
          without a lock. But it means a nil from `Create` does not prove that _this_ call wrote what
          is now on the server. Today the two writers cannot collide: step 7 parks a gated action as
          `PendingApproval` and returns, step 8 is only reached by an action that was never parked,
          and no `/approve` handler exists. The moment an approval path re-enters the pipeline for an
          already-parked action, step 8's `Create` returns nil against the parked record, the
          pre-state it just set never reaches the server, and the executor would mutate live objects
          against a journal entry carrying no snapshot and therefore no undo plan. That is the
          write-ahead rule failing in the only direction that matters, and it now fails closed.
          `TestAParkedRecordDoesNotConfirmEvenThoughCreateSucceeded` reproduces the whole sequence
          against a real API server.
          **Evidence.** 17 test functions / 30 cases with subtests (10 hermetic, 7 envtest), 100.0% statement coverage on
          `writeahead.go`, and a **19/19 mutation sweep** with zero escaped and zero broken. The
          sweep names the test that must fail for each mutation rather than accepting "the package
          went red", and runs the whole package instead of a `-run` pattern — which sidesteps
          [[LSN-048]] by construction, since a pattern that matched nothing cannot score CAUGHT if
          there is no pattern. Two of the nineteen mutate `internal/journal/store.go` rather than the
          confirmer: they are what keeps the envtest half non-vacuous, because if journal stopped
          carrying the phase into metadata or stopped folding `AlreadyExists`, the phase arm would be
          reasoning about a world that no longer exists and nothing in `writeahead.go` would have
          changed.
          **One finding filed, not a halt:** `execute/apply.go` cites **(V-REV-002)** for the
          write-ahead rule, but 09's V-REV-002 is "undo `<id>` restores prior state, verified by diff
          against the snapshot". The write-ahead check is **V-BRK-006** (05 §1.2, L2/L4, phase 9).
          Same shape as the V-BRK-020/V-BRK-021 citation defects already recorded — a comment
          pointing at a check that does not assert the property it claims. To be swept with those.
        - **P9-T7c-3d-iii-b — `classify.ActionHistory`** ✅ (2026-07-29) — the journal-derived
          novel-action source, and the two ways 06 §4.2's escalation could be switched off.
          `internal/broker/history` (new package), `classify.New`/`classify.go`,
          `policy.NewSource`. **V-BRK-029** (new, L1, BLOCKING-ALWAYS) in 09 §6.14, bound in
          `traceability.yaml` under `06§10#29` and `06§10#36`. Evidence: **100.0% statement
          coverage** under `-race`, 17 hermetic functions / 58 cases plus 6 envtest functions,
          and a **35/37 mutation sweep, 0 escaped, 0 broken**.

          **The finding that shaped the whole task: the `ActionRecord` CRD records no verb.**
          Checked against both `api/v1alpha1/actionrecord_types.go` and 06 §4.3's canonical yaml.
          So a journal-derived history cannot read `patch` or `delete` off a record, and 06 §4.2's
          "has this agent done this before" has no field to answer from. Three obvious ways out,
          all rejected with the argument recorded in the package doc:

          - **Ignore the verb** — an agent that had patched Deployments would be familiar with
            _deleting_ one. That LOWERS a risk class, which invariant 4 forbids outright.
          - **Never answer true** — strictly stricter, and vacuous: the `+1` fires on 100% of
            traffic forever, whose end state is approval fatigue. A gate everyone rubber-stamps is
            less safe than no gate, so "safe direction" is not by itself a defence.
          - **Add a CRD field** — a 06 §4.3 spec amendment, which PROTOCOL §10.5 forbids. The same
            argument `cooldown` recorded for not inventing a CRD.

          **The resolution: 06 §4.3.1's undo-strategy table read BACKWARDS.** The verb is recovered
          up to an equivalence from two durable, enum'd fields — `spec.undo.strategy` and
          `spec.undo.steps[].op` — and the equivalence is the point rather than a compromise. Every
          collapse is between operations that are **the same mutation**: `delete/delete` is the
          plan for a `create` and for an `apply` over an absent object, which is the same write.
          The pairs that must NOT collapse do not: `recreate/create` is reachable only from
          `delete`, `restore/scale` only from `scale`, and `apply` requires **both** its classes,
          so an agent that has only ever created is still novel the first time it updates.
          `TestTheUndoPlanRecoversTheVerbClass` is that table with a `familiar` and a `novel` list
          per row — it is what stops the coarsening becoming a loosening, and mutating any row is
          CAUGHT.

          **Two spec silences resolved as Decisions, not §8.5 halts.** 06 §4.2 names a
          "trust-building window" and defines none: it is **the journal's own retention window**,
          because the evidence and the window are then the same object — nothing separate expires
          and the two cannot drift. And a **dry run builds no trust**: the whole of Phase 9 is dry
          runs, so counting them would have every agent arrive at Phase 10 familiar with everything
          it had never actually done. `Undone` is excluded for the sharper version of the same
          reason — the write stood and a human reversed it, so counting it would suppress the
          escalation on exactly the repeat a human just said no to.

          **The other half of the task was the nil.** `classify.Classifier` guarded the escalation
          with `c.knownActions != nil &&`, so a broker nobody had wired a history into ran with 06
          §4.2 **off** — a risk class lowered by an omission. Four changes close it: `classify.New`
          refuses a nil; the consumption guard is inverted to `nil ||` so unknown ⇒ novel ⇒
          escalate; `classify.AlwaysNovel{}` is the deliberate spelling of "no journal"; and
          `policy.NewSource` refuses a nil at **construction** rather than letting it surface as a
          failing poll seconds after startup. Same hole ii-a closed for the accountant, one package
          over.

          **The envtest half is what makes the design a measurement.** `ActionRecord` carries a
          status subresource, so `client.Create` **drops `status` entirely** — a record created
          with `Verified` comes back with an empty phase and confers nothing until
          `Status().Update()` writes it. Every hermetic test sets that field on a struct and
          "works"; only a real server shows that the filter reads the field the server actually
          stores. This is the mirror of iii-a's finding. Two of the sweep's mutations are on the
          **CRD yaml, not the Go** — dropping the status subresource, and widening the strategy
          enum — and both are CAUGHT only by envtest tests, which is what keeps that half from
          being decoration.

          **Two mutations are recorded REDUNDANT rather than deleted.** `seen == nil ||
readAt.IsZero()` is subsumed by the staleness ceiling (a zero read time is stale against
          any real clock), so no single mutation can make an unrefreshed source vouch — it takes
          two. And teaching `class` to emit `none/<op>` changes no answer, because `verbEvidence` is
          a closed vocabulary and nothing looks that class up. Both escapes are the design working;
          recording them beats deleting the rows, since a coverage claim nobody can audit is worse
          than an honest gap. **`classify.KnownVerbs()` finally has a caller** — the lint its own
          doc claimed existed (see T7c-4) — though only a partial one: `TestEveryKnownVerbHasEvidenceDefined`
          joins it to `verbEvidence`, not yet to the envelope's enum.

          **Nothing constructs a `history.Source` yet.** Wiring is T7c-3d-iv, which must not land
          before or without T8.
      - **P9-T7c-3d-iv** — the wiring itself: a discovery client (constructed nowhere today, and
        `refindex.Source` requires it non-nil), `pipeline.New` replacing
        `broker.UnavailablePipeline{}`, `policy.Source` with a synchronous startup `Refresh` and a
        backgrounded `Run`, `cooldown.NewSource`, and `broker.NewContestedIndex`. **Closes
        LSN-007**, which needs a new L0 source assertion to close honestly: no 09 §6 check asserts
        "the pipeline is constructed in `main.go`", and `install-path-wired.py` never reads Go.
        **Split into iv-a and iv-b during survey — see the section below.**
  - **P9-T7c-4** — **the classify→execute integrity seam for `apply`, `scale` and merge-patch.**
    See LSN-040. Today only `create`, `delete` and JSON-patch `patch` traverse the pipeline; the
    other three fail closed at step 9. **Checks: V-BRK-022** (new, L1) — _every verb in the
    envelope's closed verb enum executes end to end through the assembled pipeline, with the verb
    set **discovered from the enum**_, which is LSN-040's own mechanization clause and the reason a
    hand-written table would have printed green throughout; plus **V-BRK-020** (the diff/integrity
    property this seam is the missing half of). Recon 2026-07-29 found five things the entry did
    not say:
    - **It is two fixes, not one.** `apply` refuses at `execute/integrity.go`'s `checkWholeObject`
      `default:` arm (`WholeObject=true`, which only `create`/`delete` may be); `scale` and
      merge-patch refuse at the earlier "shown no changed fields" arm (`WholeObject=false`,
      `TouchedPaths=nil`). `TestApplyFailsClosedAtTheIntegrityCheck` pins only the first; **nothing
      pins `scale` or merge-patch.**
    - **The SSOT is already named and already unwired.** `execute/diff.go`'s own doc says `Diff` is
      "called twice per action and the two calls are the whole of V-BRK-020" — call #1, before
      classification, **does not exist**. The fix is a reorder inside `pipeline.stepResolve`:
      `CaptureAll` already runs four lines later, so `snap.Live` is in hand. No new API read, no new
      interface, and the import direction already permits it.
    - **A quieter live hole than the one LSN-040 describes.** `scale` appears nowhere in
      `classify/resolve.go`, so it classifies with an empty `TouchedPaths` — meaning a
      `ChangePolicy` with `when.fieldPaths: [spec.replicas]` **can never fire on a scale**, and the
      policy author gets no error. 06 §4.2 says matching is on the touched set "**across the
      diff**", not across the submitted patch, which is the spec-level statement of the defect.
    - **`classify.KnownVerbs()` has zero callers.** Its doc says it exists "for the lint that joins
      it to the envelope's enum" and asserts "the corpus lint asserts the two agree". There is no
      such lint. Exporting `broker.validOps` for V-BRK-022 finally gives it one — two lessons closed
      by one export ([[LSN-041]] shape: prose describing a control that has never existed is worse
      than no prose, because it retires the question).
    - **Two false comments to delete while in there**, both asserting controls that do not exist:
      `pipeline.go`'s "the classifier derives those from Payload instead — so returning nil for them
      is the correct answer, not a gap" (it does not; `ScanPayload` returns `[]SecretHit`, never
      paths), and 05 §1.1's "the list is a code constant" about the dry-run carve-outs (there is no
      such constant; `SupportsDryRun` defaults to `true` and its optional hook is keyed on **ref,
      not verb**, so a `scale` cannot be recognised as a carve-out at all).
    - **One finding to file, not a halt:** V-BRK-020's row cites 03 §4.4 as its source, and 03 §4.4
      ("Reversibility as a security property") contains neither "strategic" nor "expand" nor
      "integrity". Same shape as the P9-T7c-2b halt, where 09 cites 03 §4.1 for V-BRK-021's "one
      mutating route" and 03 §4.1 does not contain it. The property is well-defined in 09 itself, so
      this is a citation defect rather than a spec contradiction.
    - **Split into 4a and 4b at SELECT, 2026-07-29.** The recon above is five findings deep and the
      task carries two independent deliverables — a **conversion** between two packages' readings of
      the same word, and a **mechanization** that discovers its own verb set. Each has its own check
      and each is checkpointable alone, which is the `harness-run` §2 test. Doing them together
      would mean a unit whose diff spans `classify`, `execute`, `pipeline` and a new lint, verified
      by one check that did not exist when the work started.

      | Unit                 | Scope                                                                                                                                                                                                                                                                                                          | Checks               | Blocks on |
      | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- | --------- |
      | **P9-T7c-4a** — done | The conversion. `apply`, `scale` and merge-patch reach the classifier with real `TouchedPaths`; the `stepResolve` reorder that makes `snap.Live` available before `classify.Resolve`; `apply` stops being `WholeObject`; the two false comments deleted.                                                       | **V-BRK-020** — pass | nothing   |
      | **P9-T7c-4b** — done | The mechanization. **V-BRK-022** — every verb in the envelope's closed enum executes end to end through the assembled pipeline, the verb set **discovered from the enum** — plus exporting that enum and the lint joining it to `classify.KnownVerbs()`. Closes [[LSN-040]] and the `KnownVerbs` prose defect. | **V-BRK-022** — pass | **4a**    |

      **The complication LSN-040 warns about dissolves on inspection.** The lesson says feeding a
      computed diff into `classify.PatchOp.Value` is lossy for the typed rules, because
      `execute.DiffResult.Ops` render `Value` as a **string** and `DirectionOfBoolField` would see
      `"true"`. But `classify.PatchOp.Value` is `any`, and for an `apply` the **desired object is in
      hand** — so the diff supplies the _paths_ and the desired object supplies the _typed values_.
      Nothing has to be read back out of a rendered string. This is only true because both inputs are
      available at the same point, which is what the `stepResolve` reorder buys.

    - **What 4a actually landed — 2026-07-29.** The dissolution above held, and the reorder was the
      whole mechanism: `stepResolve` now runs `execute.CaptureAll` before `classify.Resolve`, and
      `fillTouchedPaths` sits between them. No new API read, no new interface, one new function on
      the path.

      **It closed a live fail-closed bug, not just a classification gap.** `classify.Resolve` set
      `WholeObject` for create, **apply** and delete; `execute.checkWholeObject` accepts only create
      and delete and its `default:` arm refuses anything else. So **no `apply` could execute through
      the assembled pipeline at all.** `TestApplyFailsClosedAtTheIntegrityCheck` recorded that
      deliberately in T7c-1 and instructed its own replacement by a positive counterpart; three
      end-to-end tests are that counterpart.

      **The security property is the negative half.** `classify.matches` returns false for any
      `when.fieldPaths` rule against an empty path set, so a rule reading
      `fieldPaths: [data.log-level]` was silently inert against `apply` while reading in a policy
      review as a control in force. `TestAFieldPathsRuleFiresOnAnApply` asserts both directions,
      because a test asserting only "the rule fires" would pass equally against an implementation
      reporting **every** field as touched — the other way to make a fieldPaths rule match, and
      exactly as wrong. That implementation is mutant M3 and the negative subtest is what kills it.

      **The sweep found a hole in the check itself.** The scale expectation was written as
      `Path: scaleReplicasPointer`, comparing the constant against itself; M8 changed the constant to
      `/spec/replica` and the package stayed green. Fixed by spelling the pointer the way a rule
      author would. Final score **14/14 caught, 0 escaped**.

      **Two findings filed rather than fixed** (never in the same unit as the implementation):
      09 §6's V-BRK-020 row cites **03 §4.4**, which contains none of "strategic", "expand" or
      "integrity"; and **05 §1.1** claims the dry-run carve-out list "is a code constant" where the
      implementation has an injected `ClientApplier.DryRunUnsupported func(TargetRef) bool` and no
      `V-BRK` check asserts an envelope per carve-out kind. The second is an **unimplemented spec
      requirement**, so it is scheduled rather than edited — narrowing the doc to match the code
      would be weakening a spec, which invariant 10 forbids.

      **What 4b inherits.** The conversion is in place for every verb that arrives as an object, so
      4b's end-to-end-per-verb check has something to assert against. `classify.knownVerbs` still
      contains `"cloud"` where `broker.validOps` does not, which 4b's lint has to account for
      rather than trip over.

    - **What 4b actually landed — 2026-07-29.** V-BRK-022 exists, discovers its verb set from
      `broker.ValidOps()`, and drives all five ops through the assembled pipeline. **It found two
      more gaps the first time it ran**, both the LSN-040 shape — a package that is individually
      right, and the assembly as its first caller:

      - **`delete` could never be verified.** Every row of 04 §5.1 asserts something about a live
        object and `verify.mustGet` maps NotFound to `VerdictFailed`. A delete that worked would
        have been reported failed and rolled back **by recreating what it deleted**. Fixed with
        `verify.Target.ExpectAbsent` and an `absencePredicate` chosen by the action rather than by
        the kind, with still-present as `Pending` (deletion is asynchronous; finalizers) so only the
        settle window expiring makes it a failure.
      - **No Deployment or StatefulSet action could ever be verified.** `verifyTargets` built
        `verify.Target{Ref: r}` and dropped every other field, so `BaselineRestarts` was always nil,
        `workloadPredicate` always returned `VerdictIndeterminate`, and the settle window always
        expired into `VerdictFailed`. Every workload change that worked would have rolled back.
        Fixed by `verify.CaptureRestartBaselines` at step 3, next to the snapshots, on the same
        all-or-nothing terms — a baseline read after the write is the post-action count compared
        against itself.

      Neither was reachable from either package's own tests: `verify` tested `workloadPredicate`
      with a baseline the test supplied, and the pipeline tested a ConfigMap, whose row needs none.
      This is the entire argument for a check that discovers its verb set instead of restating it.

      **A rig defect surfaced with them.** The pipeline test rig gave `verify.Driver` a fixed clock
      and a no-op `Sleep`, so any `Pending` verdict polled forever — the `scale` case hung the whole
      package for 120s rather than failing one subtest. The clock now advances by whatever the
      driver sleeps, which is the honest fake of a clock/sleep pair and no slower.

      **Two prose defects closed, both [[LSN-041]].** `classify.knownVerbs` claimed "the corpus lint
      asserts the two agree" and no such lint existed; it is now
      `TestClassifyKnownVerbsAgreeWithTheEnvelopeEnum`, a Go test in `package pipeline` — the lowest
      package importing both `broker` and `classify` — rather than a Python lint parsing Go source.
      The `cloud` divergence is declared in code as `classify.VerbsNotCarriedByAnEnvelopeOp` with a
      written reason, and the condition making it safe is a property
      (`TestNoCloudTargetReachesTheClassifier`) rather than a sentence.

      **`verify.ErrTargetReplaced`.** A UID mismatch is evidence about a stranger for every row and
      the answer for `absencePredicate` — the deleted object is gone and something else holds its
      name. `probe.Source` now wraps a sentinel instead of prose, and the envtest that asserts the
      refusal asserts the sentinel too; that assertion is the only place the two packages' halves of
      the contract are compared.

      **Sweep: 15/15 caught** (`verification/mutants/V-BRK-022.json`), baseline green, catchers
      verified against the suite.

      **One finding filed rather than fixed.** `agentv1alpha1.ChangeVerb`'s kubebuilder marker
      (`+kubebuilder:validation:Enum=create;apply;patch;delete;scale;cloud`) is a **third** copy of
      the verb set, and its doc comment claims it "mirrors the envelope's own" with nothing
      comparing them — the same prose-as-control shape the join test just closed twice. Both
      mismatch directions fail closed today (a `ChangePolicy` naming a verb the CRD rejects is
      refused at admission; a verb the CRD admits that classify does not know matches no rule), so
      it is a finding, not a live defect. Fixing it means joining the marker to `broker.ValidOps()`,
      which is a generated-manifest lint rather than a Go test, and belongs with the other
      corpus-lint work rather than folded in here.

**Why T7c split into four.** T7c-1 was scoped as "assemble the pipeline and claim the two L1
checks", and the assembly turned out to be the small part. Three things came out of doing it.

The first is that **the two deferrals are not part of the assembly.** The `ChangePolicy` informer
and the replay route were parked on T7c because T7c was the next broker unit, not because they
share a seam with the pipeline — the informer feeds the classifier a policy set and the replay
route is a second HTTP handler. Neither is touched by wiring steps 3–11 together, and carrying them
would have made the unit oversized in exactly the way `harness-run` §2 warns about.

The second is that **the assembly and the wiring are different units with different verification.**
`pipeline.Config` has twelve dependencies that are interfaces precisely so the pipeline can be
driven at L1 with fakes; writing their real client-backed implementations is a dozen adapters whose
own property is "does this talk to a real API server correctly", which is L2. Mixing them would
have meant a unit where the L1 checks pass and the L2 half cannot run, i.e. a unit that cannot
checkpoint. T7c-1 therefore ends with the pipeline reachable from a test and not from `main`, which
is an honest partial state and is recorded as such in the `cmd/broker/main.go` comment.

The third is LSN-040, below — a gap the assembly found, which is a fix rather than part of the
assembly.

**What T7c-1 actually asserts.** `broker.StepTrace` is not a log. Its `Run` refuses to record a step
that is not the immediate successor of the last one recorded, and seals the trace on the first
refusal or fault, so "step 7 ran before step 4" and "a step ran after the pipeline stopped" are
errors the pipeline returns at the moment they are attempted rather than conditions a check hunts
for afterwards. That inversion is what makes V-BRK-014 an L1 property: fault-inject at step k and
the trace ends at k because there is no path to k+1 that does not go through a `Run` call the
failure never reaches.

The fault table covers steps 3–10 with one injected dependency failure each, step 11 has its own
test, and steps 1–2 belong to the handler and are covered in the `broker` package.
`TestEveryPipelineStepHasAFaultCase` closes the loop by iterating `broker.FirstStep..LastStep` and requiring
every step to appear — so a twelfth step added to the pipeline fails that test the day it exists
instead of quietly falling outside a hardcoded range (LSN-036). Each fault case also asserts the
world stopped, not just the trace: zero applier calls for any fault before step 9, and no record in
a phase that claims the action completed.

**One bug fixed in the pipeline itself.** `Submit` reported `Phase: string(s.verify.Phase)` to the
caller while step 11 journaled `terminal(s)`. Those agree on every path except a dry run, where the
verifier never runs: the record said `DryRun` and the HTTP response said nothing at all. Both now
derive from `terminal(s)`.

**Why T7c-2 split.** T7c-2 was "the two deferrals", and the two turned out to be unrelated in the
way that matters: one was unblocked and one is a halt.

**T7c-2b is halted on a spec contradiction.** 05 §1.3's route table names three broker routes —
`POST /v1alpha1/actions`, `.../{actionId}/approve` and `.../{actionId}/replay` — while V-BRK-021,
which is **BLOCKING-ALWAYS**, asserts "one listening port, **one mutating route**". Adding the
replay route makes the second. PROTOCOL §10.2 forbids resolving that autonomously, and the
alternative resolution is arguably the worse one: replay-as-submission (C-UC POSTs to
`/v1alpha1/actions` with `spec.trigger.undoOf`) keeps the route count at one but forces the
broker's `Authenticator.ExpectedCaller` to accept a **second identity submitting caller-supplied
operations**, which is a wider widening than a `/replay` route that accepts an action ID and no
operations at all. Sharpening the question: **09 §6 cites 03 §4.1 as V-BRK-021's source, and 03
§4.1 does not contain the phrase.** What it requires is step non-skippability, not a route count.
The narrowest question a human can answer is in the ledger's Blockers table.

**What T7c-2a asserts.** `internal/broker/policy/` is what makes `ChangePolicy` load-bearing.
Before it, `classify.FromChangePolicy` had no caller: an operator could apply a policy, see it in
`kubectl get`, see `status.agentsMatched` count the agents, and have every action classify as
though it were not there. Three things came out of building it.

The first is that **the binding predicate did not exist.** `ChangePolicySpec.AgentSelector` had zero
consumers anywhere in the Go tree. `policy.Binds` is it: `Tiers` is exact membership (a tier is a
kind of authority, not an amount of one, so a `cluster-admin` policy does not bind the
developer-team agents beneath it), `Scopes` is `scope.Contains` — "at or beneath", as the field
documents — and the two clauses are **ANDed**. An ill-formed selector scope (a hole in the middle,
which `scope.Contains` would read as a wildcard and match cluster `c` in every project) is skipped
by the predicate _and_ refuses the whole snapshot in the loader; both halves are needed, because
the first alone would be silent.

The second is that **every decision in the package follows from one asymmetry.** The classifier
takes the maximum over its sources (06 §4.2 step 3), so a policy can only ever raise a class —
which means every way of failing to see a policy is a **loosening**, and there is no symmetric
failure to trade against. So a bad policy fails the whole snapshot naming the policy rather than
being skipped; an unresolvable policy set refuses the action rather than falling back to the code
floor; and `Build` runs the same `classify.ValidateChangeRule` the admission webhook runs, so a
rule the broker would refuse and admission accepted cannot exist.

The third is that **it polls rather than watches, and the deferral's own name was the wrong
design.** The deferral said "the `ChangePolicy` informer". An informer needs a freshness signal its
own cache cannot supply — this repo already wrote down why, in `broker.MaxFreezeStaleness`: "a
watch that silently stopped delivering is not an error at all — the informer's List succeeds, the
cache answers instantly, and every answer is from before the incident started." Every way of
building that signal ends in a periodic read against the API server, at which point the cache is
buying latency and not correctness. `ChangePolicy` is cluster-scoped, human-authored and will
number in the single digits, so the source polls every 10s against a 30s staleness limit — three
polls per window, so one lost poll does not refuse and two consecutive ones do — and freshness is
true by construction. The cost is that a tightening binds within 10s instead of within a round
trip, which for a policy a human just typed is not a cost.

**The envtest run found a design flaw the unit tests could not.** The first draft treated all poll
failures alike: retain the last good snapshot, let it age out at 30s. Against a real API server
that is wrong, because two failures were being conflated. A **read** failure (the List did not
answer) is transient and retaining is right. A **load** failure (the set was read and will not
convert) is not transient at all — it will fail every poll until a human edits the object — so
aging it out means 30 seconds of classifying against a set the broker already knows is wrong, and
the operator who applied the bad policy learns about it from a delayed timeout instead of at once.
The two are now handled oppositely and the distinction is asserted from one place so the pair
cannot drift.

**LSN-007 still applies.** Like T7c-1, this lands reachable from a test and not from
`cmd/broker/main.go`, which still installs `broker.UnavailablePipeline{}`. T7c-3 is what closes it.

**Why T7c-3 split into four.** Twelve adapters is not one unit, and the reason is not only size.
Each adapter's own property is "does this talk to a real API server correctly", which is an L2
claim, and L2 claims are bought one cluster round trip at a time. A single unit holding all twelve
would have had one checkpoint at the end and no honest partial state before it — exactly the shape
`harness-run` §2 tells us to split rather than carry. The four sub-units are cut along the seams
that already exist in the pipeline: 3a is everything **classification** reads, 3b is what **undo**
needs to exist before an action runs, 3c is everything **verification** does after, and 3d is the
wiring that makes the binary reach any of it. 3d must be last because it is the only one whose
precondition is all the others; the cost is that the broker keeps 503ing until it lands, which is
LSN-007 remaining true for three more units and is recorded rather than worked around.

**Why T7c-3c split into three.** Found at ORIENT, before any code: 3c is five interfaces, and only
the first of them is an adapter in the sense 3a and 3b were. `verify.Prober` is eight methods whose
cluster mechanics have almost nothing in common — an EndpointSlice enumeration, a restart-count
aggregation that has to resolve a workload's selector, a provider read that is really a Config
Connector CR plus a node-label count, a `SubjectAccessReview`, a dry-run admission observation —
and it is the whole of 04 §5.1's evidence surface. The other four are the ladder's **effects**:
`Rollbacker`, `Pager` and `Pauser` write to the world (3c-ii), and `CooldownRegistry` needs a
durable store rather than the in-process map, which `verify/cooldown.go` already says in its own
doc comment (3c-iii). Cutting between "what verification reads" and "what recovery does" also puts
the destructive L2 surface in exactly one sub-unit: 3c-i only reads and dry-runs.

**What T7c-3c-i asserts.** V-PRO-027, newly allocated. See the check text in 09 §6.6; the argument
for allocating it rather than claiming V-PRO-013 is in this unit's ledger row.

**Why T7c-3c-ii split into two.** Not a sizing call. `Pager` and `Pauser` cannot be written as
adapters at all, and finding that out took reading one line of 06 §2.2.1: the **broker's operations
grant is read-only on `agents` and carries no verb on `events`.** V-BRK-013 asserts that grant
**exactly**, and V-BRK-013 is BLOCKING-ALWAYS. So the broker process cannot pause an agent — that is
a write to an `Agent` — and cannot page — that is an Event. A `Pauser` implemented as a client call
from the broker would need the grant widened, which is precisely the change PROTOCOL §10.2 forbids
doing to get an implementation to work.

The invariant-preserving shape is the one 05 §1.7 already names: **"exactly one code path that stops
an agent."** The broker records the intent in the journal — which it can write — and a
**controller-side C-BR reconciler** fans it out into the pause and the page, from the operator's
identity, through the single stop path that already exists. That reconciler does not exist, and
writing it is a controller unit, not an adapter unit. So ii-b is `Pager` + `Pauser` + C-BR together,
and its precondition is a design decision recorded in the ledger rather than a missing file.

Splitting here also keeps the two halves honest about their verification. ii-a's property —
V-REV-011 — is provable today at L1 and L2 against a real cluster, with no deployed surface needed.
ii-b's property is that a rung-5 escalation reaches an agent that then stops, which is an
end-to-end claim over two processes and belongs with the wiring, not before it.

**What T7c-3c-ii-a asserts.** V-REV-011, newly allocated in 09 §6.3. The clause that motivated
allocating a new check rather than extending V-REV-004 is "**replays the pre-state**": at L1 a
successful replay means a field changed, and the pre-state of a scaled-down Deployment is running
pods. That distinction is only assertable where controllers run, so the check is L1+L2 from
allocation and its L2 half shipped in the same unit.

**Why T7c-3c-ii-b split into two.** The two halves have different provable properties, different
verification levels, and — the part that actually forced it — different **preconditions**.

The request half is "a rung-5 escalation is durably recorded where `C-BR` can see it". Nothing under
test runs from a deployed image, so P1 is waived by construction exactly as it was for ii-a, and the
property is provable at L1 against a real API server in envtest.

The fan-out half is "an escalation reaches the agent and the agent stops". That is a claim about the
**operator**, which means the evidence needs the operator image rebuilt, pushed and rolled **by
digest** — P1 in full, for the first time in this task chain, and the first time in Phase 9 that a
unit's verification depends on a deploy rather than on a client connection. Bundling the two would
mean either the request half waits on an image roll it does not need, or the fan-out half ships with
P1 waived, which is [[LSN-001]] with extra steps.

The seam between them is a field, not a function call, and that is the point: the broker cannot call
the controller, because 06 §2.2.1 gives it no verb that would let it. What it can do is write
`actionrecords/status`, which it already must.

**What T7c-3c-ii-b-1 asserts.** V-REV-006 at L1 — "a failed rollback pages **and** auto-pauses the
agent", 04 §5.1, `¬`, BLOCKING-ALWAYS. Its 09 §6.3 level list is widened from `L2` to `L1, L2`:
nothing is removed, relaxed or narrowed, so §10.2 is satisfied, and the L2 half stays owed by
ii-b-2. The L1 half is not the whole check and the ledger row says so — what it proves is that the
**request** is durable and complete: both halves recorded, the reason carried, the record named, and
an escalation that cannot be written surfaced as an error rather than swallowed. The negative
control is the direction that matters: a rung the driver never reached must leave `status.escalation`
absent, because a record that claims an escalation nobody requested is how a `C-BR` reconciler pauses
a healthy agent.

**Why T7c-3c-ii-b-2 split again, into 2-a and 2-b.** The same argument one level down, and the
sizing rule made the call: the reconciler is Go plus two CEL rows plus two L1 suites, and the deploy
is a new ServiceAccount, a new grant, a new Deployment, a manager selector, an image build and a roll
by digest. Bundling them means the code half cannot checkpoint until a cluster is reachable, and a
unit that cannot checkpoint is one killed session away from being redone.

The split is only honest because the code half claims something real on its own. It does: the L1 half
of V-REV-006 was opened by ii-b-1 with the **request** and left explicitly incomplete, and 2-a closes
it with the **fan-out** — a recorded escalation becomes a patched `spec.operations.paused`, a page,
and a receipt, with the `¬` proving the converse (a record that owes nothing must leave the agent
running and emit nothing). 2-b then claims the L2 half, which is the part that needs a cluster to
mean anything: at L1 the pause is a patch against a fake API server, and "the agent actually stopped"
is not something L1 can observe.

**What T7c-3c-ii-b-2-a asserts, beyond the reconciler.** Two rows in `vap-agent-scope-journal` that
turn the broker/C-BR seam from a convention into an admission decision — C-BR may write only the
fulfilment half, and may neither create the escalation nor edit what was requested. Without the
second row the controller holding the pause verb could author the justification for using it, which
is the concentration of authority the split exists to prevent. Both rows and the broker's mirror
denial are exercised against a real API server, in both directions, and mutation-tested.

**What T7c-3a asserts.** `livestate.Source` is the adapter behind every rung of 06 §4.2's
ladder: object labels and annotations, namespace labels, the blast-radius denominator, the secret
digest set, and lower-tier ownership. Four things came out of building it.

The first is that **its five methods do not share a failure direction, and treating them alike would
have been a security bug in both directions.** `CountWorkloadObjects` is the denominator of
`AbortScopeFraction`, and `ComputeBlastRadius` turns an error into a **nil** fraction — which
disarms the abort rule entirely. So a kind the caller cannot list is **skipped, not fatal**: a
smaller denominator makes every fraction larger, which makes the abort more likely. The reflex "I
could not see everything, therefore I must refuse to answer" is the loosening direction here.
`SecretDigests` is the opposite: an empty digest set is the exfiltration gate answering "no secrets
here" to every payload, so a failed List is an error. `LowerTierOwner` is likewise fatal — "the
Agent list did not answer" must not read as "nobody owns this". Each method's doc comment carries
its own argument, because the next person to touch one of them will otherwise make them consistent.

The second is that **the fake client cannot honestly test this adapter.** controller-runtime
v0.19.0's fake tracker does not model `PartialObjectMetadata` at all — the type these reads are
built on, because a classifier has no business fetching object bodies. A fake agrees with whatever
shape the caller assumed, so a green there would be a green about code that never ran (LSN-001's
shape, one layer in). Hence a three-level split, each file stating in its header what it does **not**
attempt: hand-rolled stubs for the decision logic (which kinds are countable, which failures are
fatal, when a cache expires), envtest for "a real API server answers this way", and an L2 probe for
"a real GKE cluster with a real discovery surface and real RBAC answers this way".

The third is that **the adapter does not belong in package `classify`, and the L0 chain is what
said so.** It was written as `classify.ClientLiveState` and the first full L0 run rejected it:
V-GAT-017 holds a **closed import allowlist** over `internal/broker/classify` which deliberately
contains no Kubernetes client of any kind, because the classifier is handed already-resolved facts
precisely so that it cannot go and look anything up. Eight imports were refused at once. The
smallest diff to green was to widen the allowlist, and that is the move PROTOCOL §10.1 exists to
forbid — the check's own doc comment argues that the failure it prevents is not somebody importing
an SDK on purpose but a plausible refactor widening the list one line at a time. So the adapter
moved to `internal/broker/livestate` and became `livestate.Source`, which is the same
interface-here / implementation-there seam `internal/broker/policy` already uses for `ChangePolicy`.
Two properties depend on the split: the classifier stays hermetic, so the 165-envelope corpus can
permute every input and get a byte-identical answer, and the allowlist stays a conversation rather
than a diff. The package comment records the argument at the place someone would undo it.

The fourth is **a new pattern: `k8s-operator/test/l2/`, Go probes behind a `//go:build l2` tag.**
Without the tag the file does not exist to the toolchain, so `go test ./...`, `go vet ./...` and the
L0 chain stay hermetic and no CI runner can reach a cluster by accident. The destructive-test guard
is duplicated inside the probe rather than left to its wrapper, because a probe that creates and
deletes namespaces and can only be aimed safely by a shell script is one `go test` away from being
aimed at the live install. Its wrapper, `dev/verify/classify-live-state-l2.sh`, declares P10 and P6
and argues **in writing that P1 does not apply**: nothing under test runs from a deployed image, so
the working tree is the build under test by construction. That argument is now also the qualifier on
`dev/L2-CHAIN.txt`'s blanket P1 statement, and it names its own expiry — when 3d wires the broker,
the end-to-end successor in `broker-execute-l2.sh` needs P1 in full.

**What T7c-3b asserts.** Two adapters, one seam each: `refindex.Source` behind
`undo.ReferenceIndex`, and `bodystore.Journal` behind `execute.BodyStore`. Three things came out of
building them.

The first, and the reason the unit needed a check of its own, is that **the hard question is not
"can it find references" but "what counts as one"** — and the answer is the loosening direction, so
it is argued in the package doc where someone would undo it. 06 §4.3.1 says "every ownerReference,
PVC binding, and external reference pointing at **the old one**". What a recreate destroys is the
**UID**, so a reference bound to the UID is left dangling and a reference bound to the **name**
resolves to the new object — it is _repaired_ by the recreate, not broken by it. That collapses the
domain to UID-valued references, which in practice is `metadata.ownerReferences`. The tempting
generalization — report every reference-shaped field the scan can see — reads as extra safety and is
the opposite: on a real cluster nearly every object is named by something, so it would downgrade
nearly every `delete` to `none`, make the whole `recreate` strategy dead code, and be reported as a
tightening while it happened. A gate that always fires is indistinguishable from no gate. The
residual is written down because it is the direction that needs an argument: a UID-valued field
outside `ownerReferences` (only `PV.spec.claimRef.uid` in core Kubernetes, and unreachable anyway
because PV and PVC are on `undo.nonRecreatableKinds`, so the strategy short-circuits before the
index is consulted), and references held outside the cluster, which no in-cluster scan can see.
V-REV-010's mandatory negative control **is** that boundary: a Pod mounting the target ConfigMap by
name must change nothing.

The second is that **`refindex.Source` and `livestate.Source` fail in opposite directions, one week
apart, and neither is a copy of the other's default.** A kind `livestate.Source` cannot list is
skipped; a kind `refindex.Source` cannot list fails the entire scan, with `IsForbidden` given its
own message naming the grant that is missing. The direction is not a house style — it follows from
what a partial answer means to the caller. A missing kind shrinks the blast-radius **denominator**,
which makes every fraction larger and the abort _more_ likely, so skipping is the tightening move
there. A missing kind in a reference scan means a referrer might exist and be unseen, and the caller
reads an empty slice as "nothing points at it, the recreate is safe" — which
`undo.ReferenceIndex`'s own doc comment already forbids: "'nothing points at it' and 'I could not
look' are the two answers this package must never conflate." Both directions are mutation-tested.

The third is that **only a real cluster can demonstrate the harm, and until this unit nothing had.**
envtest runs no kube-controller-manager, so an `ownerReference` there is an annotation with no
consequences and 06 §4.3.1's premise — "the garbage collector sees owner references pointing at a
UID that no longer exists and deletes the children" — was a sentence in a spec that nothing
executed. `TestREV010TheGarbageCollectorDoesWhatTheDowngradePrevents` performs the sequence a
`recreate` plan would have performed: delete the owner, watch a real GC destroy the dependent,
recreate the owner from its snapshot, observe a new UID and the dependent still gone. That is the
state an undo reporting `done` would have left behind, and it is now on the record rather than
described. The probe fails loudly rather than skipping if no collection is observed, because "the
dependent survived" would otherwise read as evidence the downgrade is unnecessary.

`bodystore.Journal` is smaller and is [[LSN-034]] applied **before** the fact rather than after a
green run: `execute.capture` digests the body itself and compares against what the store returns,
which is only worth doing if the two numbers have independent provenance, so the adapter returns the
**sink's** digest unaltered. It calls `journal.SnapshotKey` — extracted this unit from `snapshot.go`,
which had the format string inline — rather than re-deriving the layout at a second site.

**Two findings, neither failure-driven, both carried to Deferrals rather than fixed here.** A
full-surface scan of one namespace on a 57-kind cluster takes **9.1 s**; it is sequential, O(kinds),
and sits in the request path at pipeline step 4, so a CRD-heavy cluster is worse and nothing
measures it. And **no production `journal.BlobSink` exists anywhere in the tree** — the interface has
been there since T1, `cmd/broker` passes `nil`, and `bodystore.Journal` is now complete with nothing
to talk to, so any pre-state over 1 MiB refuses its action outright. That is 03 §6's fail-closed
direction and not a hole, but it is an availability cost invisible until someone patches a large
ConfigMap, and a real sink needs a bucket, a GSA through Workload Identity and a lifecycle policy
matching 06 §4.3's TTLs — a provisioning unit, not an adapter unit.

**What T7c-3c-iii asserts.** V-PRO-028, newly allocated in 09 §6.6. Four things came out of building
it, and the first two changed an interface.

The first is that **the durable store had nowhere to live, and two of the three candidates were
unavailable rather than unattractive.** `verify.MemoryCooldown`'s doc comment pointed at
`Agent.status.operations`, which the broker cannot write: 06 §2.2.1 grants it `get, list, watch` on
`agents` and no write verb, and V-BRK-013 asserts that grant _exactly_ and is BLOCKING-ALWAYS, so
widening it is not a move an implementation gets to make. A new CRD would be a 06 §1 amendment,
which PROTOCOL §10.5 keeps out of an implementation unit. The comment was wrong on the only point
that mattered and now says so. What is left is the journal — and the journal is not a fallback: the
cooldown is **already** a function of it, because "after a failed or rolled-back remediation of a
target" is a query over `status.phase` and `spec.targets`. Storing a counter beside the records
would be a second copy of a fact they already hold, and the two would eventually disagree. This is
06 §4.4's contested-index shape and its argument — "the index is authoritative because a deleted
object cannot hold an annotation" — one control over.

The second is **the window between the rollback and the status write**, which is what put an action
ID in `verify.CooldownRegistry.Enter`. `enterCooldown` runs inside `rollBack`, before its caller
writes `status.phase`, so a purely derived registry reports "no cooldown" for exactly the interval
in which the next action arrives — whatever is driving the flap is still driving it. So `Source` is
a composition: journal plus an in-process overlay of the failures it has been told about and cannot
yet see. The union is **by action ID**, and the ID is why. Handed only a target key the store would
have to guess whether an event it sees is new, and both guesses are wrong — count it twice and one
rollback buys a doubled quiet period, count it never and the cooldown does not exist until the write
lands. A no-interface-change alternative was worked through and rejected: `max(journal, overlay)`
computed separately undercounts `consecutive` during the catch-up window (a journal holding two
prior failures plus a fresh overlay event yields 5 minutes where the correct answer is 20), and the
error is in the **loosening** direction at the moment the cooldown matters most.

The third is that **agreement with the reference implementation had to be a property, not a
convention.** A durable store that reconstructs a _different_ quiet period from the same history is
worse than no durable store, because it looks authoritative and answers differently. So the backoff
moved into `verify.CooldownSeries`, one fold with two consumers arriving from opposite directions:
`MemoryCooldown` folds events live, one per rollback; `cooldown.Source` folds a sorted slice
recovered all at once after a restart. `TestSourceAgreesWithMemoryCooldown` runs one history through
both and compares, and it guards itself — a history that left no cooldown active would make the
comparison vacuous, so the test fails on that too. The fold's two rules stopped being edge cases the
moment it had a second consumer: the sort in `seriesLocked` exists because a Go map iterates in a new
order every time and an unsorted fold would apply the decay against the wrong previous event,
answering differently on two consecutive reads of an unchanged journal.

The fourth is **the read**, which deviates from 05 §1 step 5's literal word. Step 5 says
"informer-cached"; this is a TTL-bounded snapshot over a List, for the reason `broker.MaxFreezeStaleness`
already spells out in this repo's own words — "a watch that silently stopped delivering is not an
error at all — the informer's List succeeds, the cache answers instantly, and every answer is from
before the incident started". `livestate` and `policy.Source` made the same call, and
`cmd/broker/main.go` builds a **direct** client on the same argument. Recorded as a ledger decision
rather than left as an unremarked divergence. Past `MaxJournalStaleness` the registry **refuses**
rather than reporting the target quiet, matching `broker.contestedRefusal`; inside the bound a
single dropped read ages the snapshot rather than discarding it, matching `policy.Source`. **The
residual, named because it loosens:** a rollback whose `status.phase` write never lands — the broker
is killed between the two — is a failure event no later process can recover, because nothing durable
records it. That is one action's tail against a whole process's worth of cooldowns, and closing it
would need the rollback and the phase write in one transaction, which the API server does not offer.

**What T7c-3d-i asserts.** V-CTR-017, newly allocated in 09 §6.9. Three things came out of building
it, and two of them came out of the mutation sweep rather than out of the design.

The first is that **`Observe` returning no error is not laxity, it is where the fail-closed table
lives.** `pipeline.BrakeSource` gives the observer no way to report failure out-of-band, and that is
correct: 06 §4.4 does not have a row for "the source errored", it has rows for _which input_ is
missing. So an unreadable Agent must arrive at `Decide` as a **nil Agent in the view**, not as a
returned error the caller has to remember to map back onto row 2. The consequence runs through the
whole file — `refresh` attempts all four reads even after the first one fails and `errors.Join`s the
results, because a source that short-circuits reports the row of whichever read it happened to try
first, which is a **misattributed refusal**: correct verdict, wrong reason, and the reason is what a
human reads at 3am. The same argument makes `readRoster` return three states rather than two — no
ref configured, a ref that resolves to nothing, and "I could not look" — where only the third
retains the previous answer.

The second is that **the cache degrades into row 1 by itself, and that is the reason the TTL is
bounded by a constant rather than chosen.** `FreezeView.ObservedAt` is stamped with the instant of
the **read**, never the instant of the serve, so a source whose refresh has been failing for 31
seconds hands `Decide` a view that `Decide` refuses on its own `MaxFreezeStaleness` arithmetic —
there is no liveness tracking anywhere in the source, and nothing to keep in step. `NewSource`
therefore refuses a `CacheTTL ≥ MaxFreezeStaleness` outright: a view served from cache could
otherwise already be too old for row 1 at the moment it is handed over, which would make the cache
itself the thing that freezes the fleet.

The third came out of the sweep, and it is the useful one. **The first pass was 14/20, and two of
the six survivors were real gaps rather than redundancy.** Survivor one: the only aging test in the
file failed _every_ read at once, so the freeze list went stale along with everything else and
`Decide` fired **row 1** first — the test was named for the Agent and was measuring the freeze
ceiling, which is [[LSN-035]] verbatim ("a negative control only proves the _suite_ fails; it never
proves _which rule_ made it fail"). Rewritten into three subtests that each fail exactly one read
and assert the other inputs are still present in the view, so the refusal is attributable to the
input under test. Survivor two was worse and is a security property: `readRoster`'s answered /
unanswered bool is **unobservable** until a roster that _did_ resolve goes away, because both nil
branches look identical on a source that never had one. Mutating it to `false` means a deleted
`ApprovalRoster` keeps approving gated actions from the retained copy until the staleness ceiling
catches up — **thirty seconds of approving against a roster that no longer exists.** Now covered by
`TestARosterThatDisappearsIsGoneAtOnce`, which advances the clock by only one cache TTL so aging
cannot be the cause of what it observes. Both tests were strengthened because the **sweep** found
them vacuous, not because an implementation was failing, so this is not the `harness-run` §4
coupling.

**Why V-CTR-017 rather than V-CTR-007, and why a new ID at all.** V-CTR-007 is the check whose _text_
names this property — "brake objects behave per contract, including fail-closed on unreadable
`FleetFreeze`" — and it is **L2**, because its property is a real API server refusing a real read,
which no fake client can produce. It is routed to P9-T9 with V-BRK-006 and it stays there;
`verification/results.csv` row 73 already says so in its own notes. V-CTR-015 is L1 and covers
`broker.Decide`, but it feeds the decision function inputs built by hand, so it is structurally blind
to whether the thing that builds them in production tells the truth. Neither covers this, so the
choice was a new ID or nothing — and allocating one for genuinely new coverage is a tightening,
which PROTOCOL §10 permits. Precedents: V-CTR-014 (P8), V-CTR-015 (T6b), V-CTR-016 (T6c), each on
T6b's written argument that leaving the broker's most safety-critical functions uncovered at L1
until P9-T9 means shipping them with their only check a shell script that has never run.

**The residual, named because it loosens.** Row 7 — the 04 §4.2 initiative budget and flap counters —
**cannot fire in production** after this unit. `broker.BrakeBudget`'s zero value permits by
deliberate design, and the only thing filling it is `brake.Unaccounted{}`. That is disclosed as a
required constructor field and a named type rather than a nil default precisely so it is greppable;
P9-T7c-3d-ii replaces it.

> **Superseded 2026-07-29 by P9-T7c-3d-ii-a.** The residual above was real, and the disclosure was
> the wrong instrument for it: an exported permissive accountant is a **supported way to switch a
> fail-closed rule off**, greppable or not. `brake.Unaccounted` is deleted, along with the seam it
> sat on. Row 7's blindness case now lives one level out — a nil `broker.Accountant` refuses and
> escalates, `pipeline.New` refuses to construct without one, and the only always-solvent
> implementations are test doubles in `_test.go` files. What remains for **ii-b** is an accountant
> that can answer with real numbers, not one that can be omitted.

**Why V-PRO-028 is L1 only.** Phase 9 runs entirely in `PhaseDryRun`, so no record on a real cluster
reaches `RolledBack` and there is nothing at L2 to recover from. The end-to-end property — a live
agent actually refused, and the refusals lengthening — is V-PRO-005 and V-PRO-017, both already L2
in phase 13. The distinction is written into 09 §6.6 beside the new row: those two pass perfectly
against a cooldown held only in broker memory, which is cleared by `kubectl delete pod`.

**The split is driven by level as much as by size.** Of T7's fifteen listed check IDs only five are
reachable without a cluster — V-RUN-011 (L0, L1), V-RUN-003 (L0), V-BRK-012 (L0), V-BRK-011 (L1) and
V-BRK-014 (L1). V-RUN-001/002/004/005/009 and V-ISO-001/002 are `L2` in 09 §6: they assert that the
pair actually runs, that the init container actually blocks, and that the NetworkPolicy actually
drops a packet. Those go to **P9-T9** with the seventeen already routed there, and so does
V-RUN-003's own `L2` half — its `L0` half is the hardened `securityContext` P8 already renders,
re-asserted here against the broker by T7b's goldens and by `TestBrokerDeploymentPosture`.
V-RUN-006 ("agent with no broker fails closed into observe-and-report") is `L2` and **phase 10**, so
it is claimed by nothing in phase 9; T7b's `cmd/broker` tests exercise the same clause at L1 as
supporting evidence and no more.

**Why T7b stops at the render, and T7d exists.** T7b as first written also owned the TLS Secrets,
the broker NetworkPolicy and the agent's egress-to-broker rule. Two things came out of implementing
it that make those a different unit rather than the tail of this one.

The first is that **the Secrets cannot be rendered — only the certificates that fill them can, and
the issuer they need does not exist.** 08 §2.3 wants mutual TLS between two ends that verify each
other, which means one CA signing both. The only `Issuer` in this repo is the namespaced
`selfsigned-issuer` the webhook uses, and self-signing each `Certificate` separately gives the
broker end and the agent end **different** CAs — a pair that then fails the handshake it exists to
perform. So T7d has to introduce a mesh CA `ClusterIssuer` and a CA `Certificate` under it first,
which is a cert-manager API-types dependency and a cluster-scoped object, not a line in
`broker_manifests.go`. Nothing in T7b is blocked by the gap: the Deployment mounts
`<agent>-broker-tls` and `<agent>-mesh-tls` by name, and until T7d creates them the pair stays
`BrokerReady: false` — fail-closed, which is the required direction.

The second is that **the NetworkPolicy's whole property is at L2.** Its check IDs (V-ISO-001/002)
assert that a packet is actually dropped; rendering the YAML proves nothing they ask about, and both
are already routed to P9-T9. Pairing the policy with the certificates and the actor SAs — the three
things that turn a rendered pair into a talking one — keeps one unit's worth of "the pair runs"
together instead of splitting it across a render unit that cannot test it.

Same reason for the actor ServiceAccounts. T7b derives the **name** (`<tier>-<leaf>-actor`,
truncated per 06 §5.1) because the Deployment has to name something, and pins that derivation with a
test. Creating the SA, and binding it to the empty role 06 §2.2.1 requires, is T7d's.

**Why the label renderer is its own unit and not three constants at the top of `agent_manifests.go`.**
V-RUN-011 calls a scope-label collision "an authority bug, not a cosmetic one", and it is right in a
way that is easy to under-read. 03 §4.2 pins a pod to its ServiceAccount by asserting the pod's
`kube-agents/tier`, `kube-agents/scope` and `kube-agents/role` match the SA's; 08 §2.5 keys the mesh
NetworkPolicy and the per-scope quota on the same value. A scope key is
`<project>.<cluster>.<namespace>` — 30 + 40 + 63 characters against a 63-byte label ceiling — so
**truncation is the default path, not an edge case**, and truncation alone maps two namespaces in one
long-named cluster onto one label. The pinning selector then stops distinguishing two credentials it
exists to distinguish. So `RenderScope` is built to make injectivity an argument rather than a hope:
a short legal value passes through unchanged (output _is_ input), anything else becomes a readable
prefix plus a 10-hex digest **of a length-prefixed canonical encoding** of the three levels, and a
literal that would _look_ hashed is pushed into the hashed set so the two sets cannot overlap. The
residual is stated, not hidden: a 40-bit digest collision between two scopes that also share a
52-byte prefix.

**T7a found a real defect in its own renderer, and the corpus is why.** The first draft hashed the
readable join, so `{acme, prod.eu, payments}` and `{acme, prod, eu.payments}` both rendered
`acme.prod.eu.payments` — and both were short and legal, so both took the pass-through path and the
digest never ran. The join is ambiguous; the fix is to hash a length-prefixed encoding instead, and
to require every level to be a DNS-1123 label (which forbids `.`) before allowing pass-through. The
`¬` control 09 requires is `TestTheCollisionCorpusBreaksANaiveRenderer`, which runs a naive
`truncate(sanitize(key), 63)` over the same 16-entry corpus and asserts it collides — without it, a
corpus that everything survives would prove nothing about the corpus.

**The journal's `kube-agents/scope` means something else, and T7a does not change it.** 08 §2.5
defines the key as the rendering of the whole scope key; 06 §4.3's ActionRecord examples and the
06 §5.1 ServiceAccount table use the same key for the scope **leaf** (`team-x`, `cluster-a`). These
are different objects, so it is not a contradiction — but 03 §4.2 compares a pod's value to its SA's,
and a leaf is not injective across a fleet (two clusters each with a `team-x` namespace render the
same label). T7a single-sources the **key spellings** so that `internal/journal` and the renderer
cannot drift on the string, leaves the journal's value derivation exactly as it is, and declares it
in the lint's exemption table with that reason. Reconciling the two meanings is a spec question with
a real blast radius and no check pointed at it, so it is recorded as an open item rather than settled
inside an implementation unit (PROTOCOL §10).

**V-RUN-012 ships as two halves, and the negative control is not hypothetical.**
`resolveDeploymentReplicasAndStrategy` already renders `replicas: 0` for `spec.deployment.scaleToZero`,
an unrelated idling feature. So "make pause set replicas to 0" is one `||` three lines from code that
already does exactly that, it reads in a diff as tidy reuse, and it passes every test that does not
specifically render a paused agent. The L1 half
(`internal/controller/pause_not_scale_to_zero_test.go`) renders a paused and an unpaused Agent and
asserts the Deployment specs are deeply equal — deep equality rather than a replica assertion,
because any difference rolls the pod and V-RUN-012 requires the pod UID and start time to survive.
The L0 half (`dev/tests/pause-is-not-scale-to-zero.py`) asserts the shape that survives the renderer
moving. `scaleToZero` itself is the `¬` control: the same mechanism, reached through the field that
is allowed to use it.

**A spec rule with no check and no owner, found while writing T6a's principal patterns.** 06 §1.2
**V-11** requires platform-qualified principals (`^(slack|googlechat):\S+$`) on
`Agent.spec.integration.*.allowedUsers`. It is enforced nowhere in Go, it has **no check ID anywhere
in 09**, and no task in `docs/build/` names it — P8-T9 covered V-6, V-8 and V-10 only. Not fixed
here: adding a validation rule to satisfy a check that does not exist is a different unit of work
(PROTOCOL §10), and the missing check is the larger half of the problem. T6a does implement the V-11
FORM on every principal field it introduces (`FleetFreeze.spec.requestedBy`,
`UndoRequest.spec.requestedBy`, `Approver.Principal()`), extended with a third platform, `k8s:` — a
human running `kubectl` during an outage has a Kubernetes username and no Slack ID, and a schema
that could not express that identity would make the API brake unusable in the exact failure it is
specified for.

---

## Recon 2026-07-29 — P9-T8, and the trap that T7c-3d-iv arms

Read before T8's PLAN. Recorded now because the finding is not about T8: it is about what happens to
**T7c-3d-iv** if T8 is still unbuilt when the pipeline gets wired.

**Shadow mode is declared in the API and implemented nowhere in the enforcement path.** 06 §4.1's
field table (`06:1322`) says the envelope's `dryRun` is "**Forced `true` when
`spec.operations.dryRunOnly` is set**". That join does not exist in code. Grep for any non-test
assignment forcing dry-run true returns **zero hits**. The only read of the CR field anywhere in the
tree is inside `OperationsSpec.Brake()` itself, and `Brake()`'s `dryRun` return has exactly one
consumer — `pause_not_scale_to_zero_test.go:200`. Today `dryRun` is purely a property of the
**action**, set by the caller in the envelope body (`internal/broker/envelope.go:81`), folded into
the idempotency key, and honoured at `pipeline.go:790`. The spec wants an agent-level lattice ORed
above it. There isn't one.

**Why that is T7c-3d-iv's problem and not T8's.** Phase 9 is dry-run today for a reason that has
nothing to do with `dryRunOnly`: `cmd/broker/main.go:212` still wires `broker.UnavailablePipeline{}`
and returns 503, so no execution path is constructed. That is a _situation_, not a mechanism.
**T7c-3d-iv replaces that line.** The moment it lands, the broker executes for real for any caller
that omits `dryRun`, and the operator-side switch that is supposed to prevent it does not exist.
T8 must therefore either precede T7c-3d-iv or ship with it; the ordering in the task table is not
safe as written, and this is the sharpest thing PLAN has to settle.

- **Where the OR goes.** `BrakeView` already carries the whole `Agent`
  (`pipeline/pipeline.go:84-89`), so the value is one field access from `s.brakeView =
p.cfg.Brake.Observe(ctx)` at `pipeline.go:489`. It must be applied **before** the idempotency key
  is computed: `06:2723` says reordering operations must not change the key and **changing `dryRun`
  must**. Forcing it after key verification produces either a key mismatch or, worse, a silent
  divergence between the caller's key and the broker's.
- **`agentPaused()` bypasses the guard that was written to protect it.**
  `internal/broker/brake.go:579-589` reaches into `ops.Paused` directly instead of calling
  `Brake()` — and `Brake()`'s doc comment (`common_types.go:398-403`) says in as many words that it
  exists as one function so that "a caller cannot consult `paused` and forget `dryRunOnly`, which is
  how shadow mode stops shadowing". It is the only place the broker touches `spec.operations`, and
  it forgets exactly that. [[LSN-041]]'s shape: a comment says a control exists; grep says it never
  did.
- **The `brake_controller.go:230` lead was wrong and is worth correcting in place.** `Brake()`
  returns `(paused, dryRun, reason)`, so `paused, _, _ :=` drops `dryRun` and **`reason`**, not
  `dryRunOnly`-in-slot-3. And at that site the discard is defensible: C-BR pauses a rung-5 escalation
  whether or not the agent is also shadowed, and dropping `reason` is deliberate and documented at
  `:223-227` (overwriting a human's pause reason with an automated string deletes the more
  informative of the two). The smell is real; it is just not located there.
- **No admission rule enforces stricter-only.** `dryRunOnly` appears in no webhook, no VAP, no CEL.
  The "cannot be cleared by the agent" claim rests entirely on `Agent` being absent from the actor
  templates — it is nowhere asserted as a monotonicity rule the way `ChangePolicy` and
  `requireApproval` are.
- **`status.operations.dryRunOnly` has no writer.** The observed-state mirror is dead.
- **[[LSN-040]] is literally this shape.** `execute.Request.DryRunOnly` and
  `OperationsSpec.DryRunOnly` are two fields with the same name meaning different things, joined by
  nothing — the lesson is already open and already scheduled as P9-T7c-4.

**Two stale cells in the T8 row, and two missing artifacts.** `deploy/*/scripts/` does not exist
(the MCP servers live at `agents/*/scripts/`); `internal/broker/server.go` contains no occurrence of
`DryRun` at all (the terminal path is `pipeline/pipeline.go` + `execute/apply.go`). And the unit's
premise is unbuilt: **`agents/*/skills/apply-change` exists in no tier** — all three still ship
`submit-suggestion`, which 06 §9 says is deleted — and neither `submit_action` nor `plan_action`
exists as an MCP tool.

**Check IDs.** No check in 09 mentions shadow mode or `dryRunOnly`; V-BRK-019's "dry-run" is the
server-side-apply preflight (L2, phase 10) and is a different thing. **V-GAT-019's phase in 09 §6.14
is 10, not 9** — T8 should not claim it without saying so. V-REV-001 is the reformulated-over-`DryRun`
instance already argued in planning defect 2. One genuine spec gap for PLAN: **notification under
dry-run is unspecified** — `notifyOn` is class-keyed and never mentions `dryRun`.

**Next free IDs, cross-checked against 09, `traceability.yaml` and the whole repo:** V-BRK-**023**
(022 is pre-allocated to P9-T7c-4 by this file), V-CTR-**019**, V-PRO-**030** (029 pre-allocated to
ii-b), V-RUN-**015**, V-CMP-**025**. V-CMP 009 and 012–019 exist nowhere and were never allocated —
they are gaps, not retirements (no `RETIRED` row, which §9.6 requires), so take max+1.

> **Stale as of 2026-07-30 — do not read the V-BRK number off this line.** P9-T8b-1 allocated
> V-BRK-023 from it and `dev/tests/spec-ids.py` refused the commit: 023 through **027** were taken
> between the recon and the unit (023 write-ahead confirmation, 024–027 the pipeline units), so the
> new check became **V-BRK-028**. The list was correct when written and is a snapshot, not a
> reservation. **The gate caught it, so nothing shipped wrong** — which is the argument for
> `grep -rho "V-BRK-[0-9]\{3\}" docs/ verification/ dev/ k8s-operator/ .claude/ | sort -u | tail -1`
> at allocation time rather than trusting any written-down "next free".

---

## P9-T8 ships as two units — T8a (the mechanism) and T8b (the surface)

The precedent is T3a/T3b, T5a/T5b and P8-T8a/b/c. The row is not one deliverable: it is a **join in
the enforcement path** and a **skill plus an L2 soak that exercises it**, and only the first is
buildable today.

| Unit       | What                                                                                                                                                                         | Checks                    | Blocked on                                                                                        |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------- |
| **P9-T8a** | The forcing join itself: `spec.operations.dryRunOnly` → the effective dry-run decision, in `pipeline.Submit`. Hermetic, L1.                                                  | **V-BRK-025** (new)       | nothing — **and it must precede T7c-3d-iv**                                                       |
| **P9-T8b** | The `apply-change` skill in all three tiers, the `submit_action`/`plan_action` MCP tools, `submit-suggestion`'s deletion (06 §9), and the L2 shadow soak with journal mining | V-REV-001 (L2), V-GAT-019 | **T7c-3d-iv.** Nothing executes and nothing journals in a real broker until the pipeline is wired |

**The ordering is the point, and it is the thing the recon above said PLAN had to settle.** T8b is
what the T8 row is mostly _about_, and it is the half that cannot be done. T8a is the half that the
recon showed T7c-3d-iv arms a trap for: the moment `UnavailablePipeline{}` is replaced, the broker
executes for real for any caller that omits `dryRun`, and the operator-side switch meant to prevent
that has to already exist. Shipping T8a now inverts the task table's ordering deliberately. **T8b
carries no BLOCKING-ALWAYS check**, so waiting on it costs nothing the gate will notice; V-GAT-019's
phase in 09 §6.14 is 10 regardless.

> **T8b was unblocked on 2026-07-30 and split again, into T8b-1…4.** See
> "P9-T8b splits into four" below; the row above is superseded by that table.

### What T8a found

**Nothing in the broker read `spec.operations.dryRunOnly`.** Second instance of [[LSN-007]] in this
phase: a documented field, a printer column, and a `status.operations.dryRunOnly` mirror — and an
operator who set it got an agent that executed. The field's own guard predicted it.
`OperationsSpec.Brake()` exists as one function returning all three brake values precisely so that
"a caller cannot consult `paused` and forget `dryRunOnly`, which is how shadow mode stops
shadowing" (`common_types.go:398-403`). `brake.go`'s local `agentPaused()` reached into `ops.Paused`
directly and forgot exactly that. Until this unit, **nothing in the tree called `Brake()` outside a
test.**

Four decisions, each of which had a plausible wrong answer:

- **Scoped to execution; classification is a deliberate exception.** The forced value reaches the
  executor, `stepVerify`, the terminal phase, the caller-facing message and the journaled record's
  `spec.dryRun`. It must **not** reach step 4: `classify.go:166` reads `if !in.DryRun &&
!hasUndoPlan(in)`, so feeding it the forced value suppresses the no-undo-plan escalation and the
  shadow record under-reports its class. That is both the permissive direction under invariant 4 and
  a defeat of shadow mode's purpose — a shadow is read as evidence, and one that under-reports is
  worse than no shadow at all. The sweep mutates this exception in both directions.
- **Derived, never written onto the envelope.** The obvious implementation — `env.DryRun = true` —
  works inside the package and breaks two packages away: `CompareIdempotencyKey` (`server.go:306`)
  recomputes the key over `dryRun` **before** `Pipeline.Submit` at `:344`, so every shadowed
  submission would return `400 idempotency-key-mismatch`. The recon anticipated the ordering
  constraint and got the direction backwards: the forcing must land after key verification, not
  before, and must not mutate the input it verified against. That mutation is row 15 of the sweep.
- **The field is `mayExecute`, not `dryRun`, and the polarity is the safety property.** Stored as
  permission-to-execute, the zero value — what the struct holds before anything computed the answer,
  and what a step inserted above the computation would read — is `false`, which reads back as "this
  is a dry run". A field spelled `dryRun bool` fails open on exactly the same mistake.
- **Unobservable means shadowed.** A nil `BrakeView.Agent` returns `true`. Asserted **directly on
  the predicate**, because the composed submission is over-determined: the brake's
  `agent-unreadable` row refuses a nil Agent at step 5 anyway, so a composed-only assertion would
  stay green with the predicate inverted. The over-determined claim is kept and labelled as such.

Two over-determinations surfaced this way and both were re-pointed at an assertion that can fail.
The other was the classification test: brake row 5 (`undo-plan-unusable → RaiseToGated`) gates all
three lattice rows independently, so `class` is identical whatever step 4 sees. The property lives
in `spec.classification.reasons`, and the test asserts it as an **equality against the real run**
(`reasons(shadowed) == reasons(real)`) with a third row as the non-vacuity control, rather than
against a hardcoded class that the brake would have satisfied on its own.

**Evidence.** 5 test functions / 11 cases in `internal/broker/pipeline/shadow_test.go`; `dryRun()`
and `shadowed()` at 100.0% statement coverage, `Submit` at 83.3%, package 77.0%, all under `-race`;
mutation sweep **15/15 caught, 0 escaped, 0 broken**, each row naming the specific test that catches
it and no `-run` pattern anywhere ([[LSN-048]]).

**The sweep mis-scored itself first, and that is [[LSN-049]].** Row 14's needle contains `""` (Go's
empty string literal). The sweep interpolated needles into a double-quoted `bash -c` argument, so
the quote closed early, the mutation was never applied, the python died, `&&` short-circuited — and
the run still exited 0, which the sweep printed as "ESCAPED, nothing failed" for a mutation the
suite catches cleanly. [[LSN-048]] with the sign flipped: there the tool hid a hole, here it
invented one. Needle and replacement now travel by environment variable to a helper that refuses
unless the target appears exactly once. Row 14 also caught a **mis-attribution** on the first pass —
the intuitive guess named `TestNothingComposesBackToExecuting`; the actual catcher is
`TestAnUnobservableAgentIsShadowed`. Requiring the _named_ test rather than "something went red" is
the only reason either defect was visible.

**Left for T8b or later, deliberately:** no admission rule enforces `dryRunOnly` stricter-only (it
is nowhere asserted as monotonic the way `ChangePolicy` and `requireApproval` are);
`status.operations.dryRunOnly` still has no writer; and notification under dry-run remains
unspecified in 06 §4.1 (`notifyOn` is class-keyed and never mentions `dryRun`). None of the three
are execution-path holes — the join is what T7c-3d-iv needs, and the join is what shipped.

---

## P9-T7c-3d-iv splits — iv-a (the identity seam) and iv-b (the wiring)

Surveying iv found every production adapter already exists, so the unit looked like pure
construction. One seam was not ready to be constructed against.

**What the survey found.** `policy.SourceConfig` took `Agent Agent` — a **value**, captured in
`NewSource` and passed to `Build` on every poll for the rest of the process's life. Wiring it
requires answering "where does this broker's own `(tier, scope)` come from", and the honest answer
turns out not to be a startup read:

- `--scope` carries only `scope.Of(agent).Leaf()`, a single string. `policy.Agent` needs the whole
  triple, so the value has to come from the Agent CR either way.
- `spec.tier` is immutable (webhook + CEL). **`spec.scope` is not** — `agent_webhook.go:181` says so
  in as many words: "spec.tier is immutable under V-1, and so scope is the only half of the key the
  operator can actually edit."
- A scope edit does **not** reliably roll the pod. `broker_manifests.go:368` renders
  `"--scope=" + scope.Of(agent).Leaf()`, and `Leaf()` is the deepest **set** level. Edit a
  cluster-admin's `projectId` and the leaf is still its `clusterName`: no rendered argument changes,
  no rollout happens, and a pinned identity is stale for the life of the pod. Only the platform
  tier — whose leaf _is_ its `projectId` — would be rescued by the rollout.

**Why that is not a small staleness.** `Binds` ANDs the tier clause with
`scope.Contains(policyScope, agentScope)`, and a ChangePolicy can only **tighten**. So a binding
that is _lost_ is the loosening direction: the broker classifies lower than the operator wrote, and
the record's `policySources` omits the policy without an error anywhere. Three separate inputs lose
bindings the same silent way — a stale scope, an ill-formed scope, and the zero Agent.

**And the codebase had already answered this question the other way.** `pipeline.callerScope` reads
`scope.Of(p.cfg.Brake.Observe(ctx).Agent)` on **every submission**. Pinning in `policy.Source` would
have made it the one place the broker's own identity was frozen — an inconsistency neither side
reveals when read alone. Wiring the pinned field would have been the second half of the trap
[[LSN-041]] describes: a seam that looks wired, is wired, and is wired to the wrong thing.

| Unit               | Scope                                                                                                                                                                  | Checks                     | Blocks on |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- | --------- |
| **P9-T7c-3d-iv-a** | `SourceConfig.Agent` → `Identity func() (Agent, error)`, resolved once per poll; `Build` refuses an ill-formed own scope; nil `Identity` refused at construction. L1.  | **V-BRK-026** (new)        | nothing   |
| **P9-T7c-3d-iv-b** | The wiring: discovery client, `pipeline.New`, the remaining sources, and the reflection-over-`pipeline.Config` assertion that closes [[LSN-007]]. **Done 2026-07-29.** | **V-BRK-027** (new) — pass | **iv-a**  |

### The distinction iv-a is built on

`Scope{}` is a **legal identity**, not an error value. `validateScopeAndParent` returns early for the
platform tier — "projectId is conventional but scope may be nil here" — so a scopeless platform
agent genuinely narrows nothing, and `Scope{}.IsWellFormed()` is true. "Fleet-wide" and "the Agent CR
could not be read" are therefore **the same value and different facts**, which is why `Identity`
returns an `error` rather than a zero `Agent`: collapsing them would make an unreadable CR classify
as the widest agent in the fleet. It is the same shape as T8a's `shadowed()` — an unobservable Agent
must not read as the permissive answer — and the negative control is the same kind too: the zero
scope must still classify, which is what stops the other assertions from being satisfied by
"refuse anything not fully narrowed".

The two failure classes stay apart on the axis the package already uses. An **unreadable** Agent CR
is transient (usually it _is_ an unreadable API server), so it is **retained** and aged out on
`MaxPolicyStaleness` — same clock, no second timer. An **ill-formed** scope was read successfully
and is unusable until a human edits it, so it is **discarded** and refuses immediately.

### What iv-a left for iv-b

- The `Identity` closure itself. iv-a defines the seam; nothing constructs one outside a test yet,
  so `main.go` still holds `broker.UnavailablePipeline{}`. The intended closure reads through the
  brake — `func() (policy.Agent, error) { v := brakeSrc.Observe(ctx); if v.Agent == nil { return
policy.Agent{}, errors.New(...) }; return policy.Agent{Tier: ..., Scope: scope.Of(v.Agent)}, nil }`
  — which reuses the TTL'd read the brake already performs rather than adding a second watcher.
- **A finding, not a fix.** `pipeline.go:412` guards `callerScope` with `IsWellFormed()` only, and
  `Scope{}` passes it. A nil `BrakeView.Agent` therefore yields the fleet-wide caller scope at
  step 3 — the permissive direction — and is caught downstream only because the brake's
  `agent-unreadable` row (`brake.go:203`) refuses at step 5. Composition saves it; the classification
  and the record written before that point are still built against an identity nobody read. Recorded
  rather than fixed here, because changing classification inside a wiring unit is exactly the mixing
  the protocol forbids. Sweep it with the V-BRK-020/021 and V-REV-002 citation defects.
- **A second finding.** No production `journal.BlobSink` exists — only the interface, plus
  `WriterSink`/`MemorySink`, which implement the _different_ `AuditSink`. The >1 MiB `objectRef`
  path (06 §4.3) has no implementation, so `BodyStore` and the rollback `Sink` must stay nil at
  iv-b. That is documented-legal ("a step that needs it then refuses by name rather than by nil
  dereference") and it is why those two fields need allowlist entries in iv-b's reflection check.

### What iv-b actually landed — 2026-07-29

`cmd/broker/wiring.go` (new) + the three-line change in `run` that replaces
`broker.UnavailablePipeline{}` with the pipeline it builds. **V-BRK-027** is the check.

Everything predicted above held: the identity closure reads through the brake, `BodyStore` and the
rollback `Sink` are nil with allowlist entries carrying the BlobSink reason, and `pipeline.go:412`
was left alone. Three things the survey had not predicted:

- **The assembly needed its own file, not eighty more lines in `run`.** `run` dials a kubeconfig, a
  clientset and a TLS keypair before it builds anything, so a `pipeline.Config` assembled inline is
  unreachable from a test — and a check that cannot see the wiring is no defence against the lesson
  it exists for. `pipelineConfig` is a function whose whole output is the config.
- **Order turned out to be load-bearing, so it is asserted.** `brake` must be refreshed before
  `policy`, because the policy source's first `Refresh` calls the identity closure, which reads the
  brake's cache. Get it backwards and startup fails naming ChangePolicy for a problem that is the
  Agent CR — the wrong RBAC rule, in the one message an operator will read. The five sources became
  an ordered `[]startable` with a fatal first read; pollers start only after all five reads succeed.
- **Discovery is not in the 06 §2.2.1 grant and does not need to be.** The grant has no
  `nonResourceURLs` and the VAP refuses any that does, but Kubernetes binds `system:discovery` to
  `system:authenticated`, so the two enumerating adapters get `/api` and `/apis` without the grant
  widening — which is why it can stay byte-identical across tiers.

**The mutation sweep found a hole in the check itself** (13 mutants, all now caught). M9 hoisted
`go s.run` above the refresh and the "no poller is left running" assertion stayed green: it used a
non-blocking `select ... default`, which only observes goroutines the scheduler happened to have run
already. Asserting the **absence** of an event needs a bounded wait, not a poll. Fixed before the
check was recorded. Also: a mutant that does not compile scores as an escape, which is how M2's
first form was caught — `Contested: nil` orphans the `broker` import, so it never built.

**Carried, still not fixed:** the `pipeline.go:412` `Scope{}` finding above; the V-BRK-020/021 and
`execute/apply.go` V-REV-002-for-V-BRK-006 citation defects; and `ContestedIndex` is wired **empty**
and known to be — rebuilding it from `ActionRecord.status.contested` is P9-T6c's, which is still not
scheduled anywhere. Empty answers "not contested" for everything, which is the loosening direction;
the only alternative available today is nil, and nil makes the brake refuse every action.

---

## P9-T8b splits into four — the survey that forced it

T8b was unblocked by T7c-3d-iv-b. Surveying its surface before starting it showed the row is four
deliverables wearing one name, and two of them cannot be checkpointed in a session:

- **`submit-suggestion`'s retirement** touches **~110 files** — all three tiers' `SKILL.md` and
  `scripts/submit_suggestion.py`, `dev/test_submit_suggestion.py`, `examples/gitops-repo/`,
  `agent_manifests.go`, `internal/testing/`, `verification/traceability.yaml`, `INSTALL.md`,
  `deploy/shared/defaults/config.yaml`, the design docs and the site. And 07 §2 phases the per-tier
  replacement as **P10-T3**, so doing it here is pulling Phase 10 work into Phase 9.
- **The L2 shadow soak** needs a live scratch cluster, rebuilt agent images through Cloud Build, and
  journal mining over real records. That is its own session and its own preconditions (P1, P3, P4).

The remaining two halves are hermetic and each is a unit. The split:

| Unit               | What                                                                                                                                                                                                  | Checks                                                                       | Blocked on             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------- |
| **P9-T8b-1**       | The agent-side **envelope builder**: JCS, the §4.3.1 sanitizer, the 06 §4.1 operation sort, and the `idempotencyKey` — in Python, byte-identical across all three tiers, hermetic                     | **V-BRK-028** (new)                                                          | nothing                |
| **P9-T8b-2**       | `submit_action` / `plan_action` as MCP tools on top of the builder: nonce fetch, mTLS + projected-token transport, `trace`/`requester` from the session, `ActionResponse` rendering                   | **V-BRK-029** (new)                                                          | T8b-1                  |
| ~~**P9-T8b-3**~~   | ~~The `apply-change` skill in all three tiers, and `submit-suggestion`'s retirement (06 §9, §10)~~ — **split, see below**: the skill is Phase 9's, the retirement is Phase 10's                       | ~~V-GAT-019 (phase **10**)~~ — mis-bound                                     | —                      |
| **P9-T8b-3a**      | The `apply-change` skill in all three tiers, alongside `submit-suggestion` — **done 2026-07-30**                                                                                                      | **V-CTR-020** (new)                                                          | T8b-2b                 |
| **P9-T8b-3b**      | `submit-suggestion`'s retirement — **deferred into Phase 10 as P10-T3**                                                                                                                               | —                                                                            | Phase 10               |
| ~~**P9-T8b-4**~~   | ~~The L2 shadow soak with journal mining~~ — **split, see below**: the broker has no deployment path, so there is nothing to soak yet                                                                 | ~~V-REV-001 (L2)~~                                                           | —                      |
| **P9-T8b-4a**      | The broker's deployment path, and the L2 claim it makes checkable                                                                                                                                     | **V-BRK-012 (L2)**                                                           | a live scratch cluster |
| ~~**P9-T8b-4b**~~  | ~~The L2 shadow soak with journal mining~~ — **split again, see below**: nothing in `dev/` can present a credential to a broker, so there is no caller to soak with                                   | ~~V-REV-001 (L2)~~                                                           | —                      |
| **P9-T8b-4b-i**    | The in-cluster envelope driver, and the five transport checks it makes answerable — **done 2026-07-30**                                                                                               | **V-BRK-007/008/009/010/017 (L2)**                                           | T8b-4a                 |
| **P9-T8b-4b-ii-1** | Step 3's live reads answer a typed refusal, split by whether retrying can help                                                                                                                        | V-BRK-031 (L1, L2)                                                           | T8b-4b-i               |
| **P9-T8b-4b-ii-2** | The L2 shadow soak with journal mining, over the read-only tenant overlay                                                                                                                             | V-REV-001 (L2)                                                               | T8b-4b-ii-1            |
| **P9-T8b-4c**      | `session_trace()` emits `parentSpanId`, which the broker's closed schema refuses; fix the shipped client across all three tiers and add the assertion that would have caught it — **done 2026-07-30** | **V-BRK-032** (new, 09 §6.14) + **V-BRK-028** and **V-BRK-029** strengthened | —                      |
| **P9-T8b-4d**      | `trigger` becomes a parameter of `submit_action`/`plan_action` per 06 §9, across the three tiers' MCP tools and the `apply-change` skill that teaches them — **done 2026-07-30**                      | **V-CTR-020** and **V-BRK-029** strengthened; **V-BRK-032** extended         | T8b-4c                 |

**Why T8b-1 is the first half and not an arbitrary slice.** Everything downstream is transport and
prose; this is the only part with a _correctness_ obligation the broker will enforce. The broker
**recomputes** `idempotencyKey` and `CompareIdempotencyKey` refuses a mismatch — so an agent-side
builder that diverges by one byte does not degrade, it makes **every write in the fleet refused**,
with a message about a key rather than about the divergence. And the divergence is not hypothetical:
the key is computed over the operations _after_ `journal.Sanitize`, so a Python side that forgets to
digest a Secret's `data` gets a different key **and** has credential material in the hash input.

**This is a second definition site, deliberately, and the join is the check.** [[LSN-040]] and
[[LSN-041]] both say a second copy of a rule is only allowed when something mechanically compares it
to the first. There is no way to avoid the copy — the agent image is Python, the broker is Go, and
06 §9 puts the key computation in the MCP tool. So the copy is made and joined:
`verification/fixtures/envelopes/valid/` already carries **six envelopes, each with the key its own
operations hash to** plus `identities.json`, and `TestValidFixtureIdempotencyKeys` pins the Go side
against exactly that corpus. **V-BRK-028 runs the Python builder over the same six files and asserts
the same six keys.** No golden file, no second corpus, nothing to drift: the two implementations are
compared through an artifact that already exists and that the Go test already depends on. The corpus
is not incidental to this choice — it covers a Secret `apply` (the sanitizer), a selector fan-out
delete and a three-operation envelope with mixed verbs (the sort order), which are the three places
a re-implementation actually goes wrong.

**Where it lives.** `agents/<tier>/scripts/action_envelope.py`, byte-identical across the three
tiers — the shape `platform_mcp_server.py` and `agent_common_server.py` already have. The tests go
in **`dev/test_action_envelope.py`**, one copy, parameterised over all three tiers, rather than
three copies under `agents/*/scripts/`: that placement makes the tier-parity assertion free and
picked up by `python3 -m unittest discover dev`, which is already an L0 chain line — so no
`L0-CHAIN.txt` edit is owed. Nothing about tier parity is currently enforced for `agents/*/scripts/`
at all; the three copies of `platform_mcp_server.py` are identical by luck. That is a finding, filed
below, not fixed here.

**`agentIdentity` is the scope string, not the SA username.** `identities.json` reads
`platform/adamparco-kage` and `developer-team/adamparco-kage/gke-scratch-kube-agents-dev/checkout` —
the agent's own scope identity, which the pod knows from its rendered config. The broker's
`Identity.Username` (`system:serviceaccount:…`) is the _authentication_ subject and is a different
string; a builder that used it would compute keys nothing accepts. This is the sort of thing that is
obvious once seen and invisible from the spec text, so it is written down here.

### P9-T8b-2 splits again — the pod knows its tier and not its scope

**Recorded 2026-07-30, at SELECT for T8b-2.** T8b-1's builder refuses to compute a key without an
`agentIdentity`, deliberately: `compute_idempotency_key` raises rather than defaulting, because a
defaulted identity produces a well-formed key for the wrong agent. The obvious next question is
where the pod gets that string, and the survey found it cannot.

`agentIdentity` is `<tier>/<leaf>` — the format is `Identity.AgentIdentity()` in
`k8s-operator/internal/broker/rejection.go`, including its one-armed case for an empty scope. The
broker learns both halves as **startup flags**: `broker_manifests.go` renders
`--tier=agentindex.EffectiveTier(agent)` and `--scope=scope.Of(agent).Leaf()`. The agent pod is
rendered from the same CR a few hundred lines away and gets **`AGENT_TIER` and nothing else** — no
scope leaf, in any env var, in the rendered ConfigMap, or in the golden manifests. So the pod holds
half the identity, and the half it is missing is the one that differs between two agents of the same
tier.

**This is fail-closed, which is why it survived to be found here.** A wrong `agentIdentity` produces
a key the broker's recomputation refuses, so nothing unsafe happens — it is a total outage of the
write path dressed as a per-request 400, and it would land the first time anyone called
`submit_action`. Not an escape from shipped code: nothing imports the builder yet.

| Unit          | What                                                                                                                                                                                 | Checks              |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| **P9-T8b-2a** | The operator renders the agent's own identity into the agent pod, **joined** to the two flags the broker is started with, so the two cannot drift                                    | **V-BRK-030** (new) |
| **P9-T8b-2b** | The `submit_action` / `plan_action` MCP tools on top of the builder: nonce fetch, mTLS + projected-token transport, `trace`/`requester` from the session, `ActionResponse` rendering | **V-BRK-029**       |

**Why 2a is its own unit and not a line inside 2b.** It is the only part that changes the operator,
which means golden manifests, an envtest run and `Run Controller Tests` — a different substrate from
2b's Python entirely. And it is the half with a _drift_ obligation rather than a behavioural one: the
value has to equal what the broker was started with, forever, and the check that says so has to read
**both rendered manifests from one CR** and compose them through the production
`Identity.AgentIdentity()` rather than restating the `<tier>/<leaf>` format a third time ([[LSN-036]],
[[LSN-041]]). Bundling it under 2b would put that join inside a unit whose failures are all about
TLS and nonces, where a format regression reads as a transport bug.

**Where it goes: `agentBrokerEnvVars`, with the other five.** Not the general env block. Those five
are appended _after_ `mergeEnvVars` specifically so `spec.deployment.env` cannot win against them,
and the identity belongs to the same class: a CR author who could set it could not forge an identity
— the broker derives its own and refuses a mismatch — but they could make **every write from that
agent refused**, which is a denial of service authored in a field that looks like configuration.

---

### P9-T8b-3 splits in two — the skill is Phase 9's, the retirement is Phase 10's

**Recorded 2026-07-30, at SELECT for T8b-3.** The row above says "the `apply-change` skill in all
three tiers, and `submit-suggestion`'s retirement" and flags the whole thing as P10 work. Sizing it
showed the row is two units with opposite phase homes, and that the flag is right about one half and
wrong about the other.

**The skill belongs to Phase 9, because Phase 9's own task list asks for it.** P9-T8 is "the agent's
`apply-change` path submits real envelopes", and acceptance **(a)** is "an envelope flows end-to-end
in shadow mode". T8b-2b shipped the two tools; nothing yet tells an agent they exist, what an
operation looks like, or that it may not claim its own risk class. The soak in T8b-4 has nothing to
soak until that prose exists.

**The retirement belongs to Phase 10, and not for bookkeeping reasons.** 07 §2 phases it as P10-T3,
per tier — "turn shadow mode off for this tier and let the broker execute; wire the `apply-change`
skill (replacing `submit-suggestion` for this tier)". Retiring it in Phase 9 would delete the only
working write path in the product during the one phase whose defining property is that **no agent
holds write authority anywhere**. The replacement runs in dry-run by construction, so the fleet
would be left with a retired GitOps path and a no-op imperative one — every tier unable to change
anything, in a phase 07 §2 requires to be "independently shippable and leaves the system working".
07 §5's rule is the same rule in test form: replaced, never deleted, and swapped for its counterpart
**in the same phase that removes it**. Phase 9 cannot supply the counterpart; that is what Phase 9
_is_.

So `submit-suggestion` stays, and `apply-change` lands beside it. The two coexist for exactly one
phase, which is what a conversion looks like when the ordering rule is obeyed.

| Unit          | What                                                                                                                       | Checks              |
| ------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| **P9-T8b-3a** | The `apply-change` skill in all three tiers, alongside `submit-suggestion`, over the tools T8b-2b shipped                  | **V-CTR-020** (new) |
| **P9-T8b-3b** | `submit-suggestion`'s retirement — **deferred to Phase 10 as part of P10-T3**, per tier, as each tier's shadow mode is off | V-GAT-019 is not it |

**The row's check binding was wrong, and that is a finding rather than a renumbering.** T8b-3 cites
**V-GAT-019**, which is _parked-record completeness_ — intent, targets, rendered diff, class, the
gating rule id, and an undo plan or an explicit `undoable: false`. That is a property of a gated
`ActionRecord`, which is why its phase is 10: Phase 9 parks nothing. It says nothing about a skill,
and no skill can make it pass or fail. Nothing in 09 §6 covered the agent's own instructions for
using the write path at all, so **V-CTR-020** is allocated rather than reused — V-CTR is contract
conformance, which is where V-CTR-011's "no brake tool in any agent tool registry or skill manifest"
already lives.

**What is actually checkable about prose, and what is not.** A skill is instructions for an LLM, so
most of it cannot be asserted. Three things can, and each is a join rather than a reading:

- **The tools it tells the agent to call are tools that exist** — the names are read out of
  `platform_mcp_server.py`'s `@mcp.tool()` functions by AST, not listed in the test. A skill naming
  a tool the server does not register sends the agent to a dead call, and the symptom is an agent
  reporting that it cannot act, in prose, with no error anywhere.
- **The parameters it promises are the parameters the tool takes** — read from the same AST. This is
  the drift that actually happens: the signature changes and the prose does not.
- **What it says the agent cannot influence is genuinely absent from the signature** — tier, scope,
  risk class, approval. 02 §2.2 puts this in the persona's own voice ("the agent does not decide its
  own risk level and must never claim to"), and the reason it is worth checking is that the sentence
  is only true while the parameter is missing.

The rest — no `kubectl`/`gcloud`/`git push`/`gh pr create` anywhere in the body — is a grep, and a
weak one on its own. It is included because it is the one property the conversion is _about_: the
old skill's entire body is git and `gh` commands, and a copy-paste that left one behind would be a
mutating shell-out sitting in the instructions of an agent that holds no credential to run it, which
fails confusingly rather than safely.

---

## Recon 2026-07-29 — P9-T9, and the real size of the gate

**The Phase-9 gap, derived rather than remembered.** Parsing every row of 09 §6.1–§6.14 with
`Phase == 9` gives **50 checks**. Cross-joined against the 108 data rows of `verification/results.csv`,
counting a pass only at a level the catalog lists for that check (this file's own rule: "a property
proven at a level the check does not list is not that check passing") and retracting on a later
`**correction**` at the same level:

|                                                      |                                                |
| ---------------------------------------------------- | ---------------------------------------------- |
| Phase-9 checks in 09 §6                              | **50**                                         |
| Closed (every listed level has an un-retracted pass) | **15**                                         |
| **Open**                                             | **35** — 20 BLOCKING-ALWAYS, 15 BLOCKING-PHASE |
| Of those, zero `results.csv` rows of any kind        | **13**                                         |

**This supersedes the "22 open / 9 BLOCKING-ALWAYS" figure used earlier in this session, which does
not reproduce under any reading.** One judgement call is flagged: `results.csv` row 62 is a
`correction` on V-GAT-001 at L1 but is a re-record over a _larger_ corpus (181 cases, was 165), so it
is counted as a pass; counting it as a retraction gives 36 / 20 / 16.

**33 of the 35 gaps are the L2 level — but three are not, and those are the surprise:**

- **V-CTR-005 and V-CTR-006 are L1-only and have never been recorded.** Envelope schema round-trip;
  `ActionRecord` lifecycle transitions. This is L1 work that slipped past T1 and T2, and it needs no
  cluster.
- **V-RUN-010 is L0-only and unrecorded** — broker supply-chain minimality (no LLM SDK, no
  `/bin/sh`, one listening socket, mounts exactly {cert Secret, projected token}). An L0 lint nobody
  has written.
- **V-BRK-021 needs both L0 and L2**; its only evidence is L1. Its L0 half is a lint over the shipped
  image, and it is entangled with the unresolved P9-T7c-2b halt.

> **Superseded 2026-07-30 by P9-T9b-4** — the V-BRK-021 bullet only. It was accurate on 2026-07-29
> and both halves of it are now wrong, which is the reconciliation T9b-4 owed; the full argument is
> in "P9-T9b-4 — outcome" below. Short form: the L0 half went green that morning under P9-T7c-2c
> (`results.csv` row 138) after the row was reshaped away from a route count, so "its only evidence
> is L1" describes a file that no longer exists. And the L1 row (row 54) was never one of the
> required levels — 09 §6 lists V-BRK-021 as **L0, L2** — so it neither discharged anything nor
> constituted the gap. The real gap is the **L2** half and only that, and it is not "a lint over the
> shipped image": a lint is what the L0 half already is. The L2 half is a probe of a **running**
> broker, which is the only thing that can distinguish what the tree says from what was shipped.
> The other two bullets stand as written and both were closed by T9b-1/2/3.

**Two of planning defect 2's three guards are not in the state that paragraph assumes.**

1. **Guard 1 does not exist.** `invariants-gate.py` has 18 `check_*` functions and none mentions the
   test-only overlay. The lint that refuses an overlay reference under `k8s-operator/scripts/`,
   `deploy/` or `config/` still has to be written — and it is the one guard the paragraph itself
   calls "the single worst outcome of this decision".

   > **Closed 2026-07-30 by P9-T8b-4b-ii-2a**, and confirmed by T9b-4's gate arm.
   > `check_test_only_grants_are_confined` (**V-CTN-037**) exists, is registered, and runs on every
   > PR through `dev/L0-CHAIN.txt`. It is also stronger than the bullet asked for: rather than
   > refusing an overlay reference under three named install-path roots, it discovers **by the
   > marker** `kube-agents/test-only-grant` and asserts five properties — the marker appears only
   > under `dev/` or in prose, every RBAC document under `dev/` carries it, nothing outside `dev/`
   > names a file containing one, no marked document is cluster-scoped, and no marked Role reaches
   > `escalate`/`bind`/`impersonate`/`*` or the RBAC API group. Discovery by marker rather than by
   > path is what keeps it from being a headcount of the one fixture anybody remembers ([[LSN-036]]).
   > Its stated non-claim stands: RBAC written as a heredoc inside a `dev/**.sh` is out of scope,
   > because a heredoc's disposition is not statically derivable.

2. **Guard 3 collides with [[LSN-045]], which was learned after it was written.** "The L2 script
   asserts the namespace is empty of `ActionRecord`s at teardown" cannot be satisfied by deleting the
   namespace — `kube-agents-journal-retention` denies DELETE and strands it `Terminating` permanently
   — nor by deleting the records, which is denied until `status.exported.confirmed` is true, and
   fabricating that field was **declined as a standing rule**. Guard 3 needs re-stating as an
   assertion over labels within a reused namespace, in the `brake-fanout-l2.sh` idiom.

**`verify-phase8.sh` is the template, and five of its properties are load-bearing.** Lettered
sections bound to Accept bullets. Section A runs `dev/L0-CHAIN.txt` **as a file**, never as a copied
list, with a shrink guard (`< 13` lines ⇒ fail) so a chain that lost lines cannot read as green.
`run_l2` is the single place a sub-suite's rc is interpreted — `0` pass · `3` defer (never a pass) ·
`2` could-not-run · `*` fail. Section E detects the phase's own unfinished work **by artifact rather
than by memory**, so it flips green when the work lands and cannot be talked into it — that is the
mechanism P9-T9 should reuse for its own open items. Section F regresses through the predecessor
gate; section G prints deferrals and never asserts them green. There is **no default target** and
**no per-check-ID machine-readable output anywhere in any phase gate** — section E's hard-coded
per-ID arm is the closest thing, and `results.csv` is written by the harness agent, not by any
script.

**`dev/L2-CHAIN.txt` decision T9 owes.** Twelve executable lines, each carrying its own `--context`
even though all twelve now carry the same one (the argument against a run-loop default is written
into the file). **`verify-phase8.sh` is absent from the chain** — it runs `verify-phase7.sh` and then
the individual P8 suites. T9 must decide whether `verify-phase9.sh` becomes a line or replaces the
standing-regression line. The P1 narrowing at `L2-CHAIN:38-51` exempts the four Phase-9 client-side
probes in writing, each naming **`broker-execute-l2.sh` as the successor that needs P1 in full**.

**None of the five deliverables exist**, and `dev/verify/fixtures/` does not exist as a directory.

---

### P9-T9 splits: T9a is the trigger, T9b is the gate

**Recorded 2026-07-30, at SELECT.** The recon above says to split the review-gate path filter off
early. Done — **P9-T9a** is that unit and it is closed; **P9-T9b** is the consolidated gate and
everything else the T9 row names. Two arguments for the ordering, and the second is the load-bearing
one:

- The filter needs no cluster, and every day it stays wrong is a day of Go security surface merging
  unreviewed. It has been wrong since PR [#33](https://github.com/adamparco/kube-agents/pull/33).
- **Every commit invalidates P1 for every L2 suite still to come** (see "Notes carried into
  IMPLEMENT"). So all remaining L0 work belongs in front of the remaining L2 work, not interleaved
  with it: land T9a, then build images once, then run T8b-4 and T9b's L2 sections against a tree
  that has stopped moving.

**What T9a asserts, and why it is a derivation rather than a longer list.** Widening the filter by
hand fixes today and rots tomorrow — the filter was correct when it was written, and what changed
was the repository, not the YAML. So the security surface is **derived** from three sources that
are each maintained for their own reasons: Go files carrying a `+kubebuilder:rbac`/`:webhook`
marker, Go files that build a `tls.Config` or issue a `TokenReview`/`SubjectAccessReview`, and
manifests declaring an authority-granting kind. 65 files today. A new package that authenticates
anything, or a new directory holding a ClusterRole, joins the required set the moment it is written
and the check goes red until the filter reaches it.

**The check is one-directional and says so.** It can prove a glob is missing; it cannot prove one is
unnecessary, and `- "**"` would satisfy it. That is deliberate: over-triggering costs CI minutes and
under-triggering costs a review, so the rule only pushes in the direction where being wrong is
cheap. Two globs in the widened filter (`k8s-operator/api/**`, `deploy/**`) are judgement rather
than derivation, and are marked as such in the workflow so the next reader does not mistake the list
for something wholly generated.

**The matcher is calibrated against GitHub, not against a reading of GitHub.** The whole answer
turns on one clause — whether a leading `**/` may match zero directories, which decides whether
`**/agents/**` ever reached `agents/platform/SOUL.md`. Rather than assert the documentation, the
test replays two runs that happened: PR #33, where the gate did not fire, and PR
[#79](https://github.com/adamparco/kube-agents/pull/79), where it did, both against the filter as it
stood at the time. If the matcher's reading of `**` were wrong in either direction, one of those
flips.

**Found by a gate, not by a reader.** The first version of the check enumerated the repo with a bare
`git ls-files`, and `invariants-gate.py` refused it on the spot under [[LSN-050]]: `ls-files` without
`--others` lists the index, so the check would have been blind to precisely the new, never-reviewed
file it exists to find — the same defect it is about, one level up. Now `gitcorpus.repo_files`.

**Split this off early: the review-gate path filter is self-contained and needs no cluster.**
`.github/workflows/review-gate.yml:11-20` still matches nothing under `k8s-operator/internal/**`. It
is V-MET-007 — the one check ID the T9 row names explicitly — it does not depend on the gate work,
and it is the reason the security gate never ran on the broker. Doing it inside the gate unit means
doing it late; doing it in its own unit means it stops being true sooner. It still must not be done
in a unit that would be reviewing itself.

---

### P9-T9b splits into five: T9b-1 … T9b-5

**Recorded 2026-07-30, at SELECT, under `harness-run` §2 sizing.** T9b as written is not one unit.
Its recon says "None of the five deliverables exist"; since then guard 1 has landed as
**V-CTN-037**, and `broker-auth-l2.sh` with `fixtures/actor-tenant-grant.yaml` has landed too, so
the residual is smaller than the row but still five separable pieces of work with different levels,
different preconditions, and no dependency between the first three.

Split, in the order they will be done. **The ordering is the phase's own rule, not a preference:**
every commit invalidates P1 for every L2 suite still to come, so all remaining L0/L1 work goes in
front of all remaining L2 work.

| Unit      | What it is                                                                                                                                                     | Checks                                            | Level | Blocker                       |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ----- | ----------------------------- |
| **T9b-1** | The `ActionRecord` phase lifecycle as enforced data, at the journal's two write points                                                                         | V-CTR-006                                         | L1    | —                             |
| **T9b-2** | Envelope schema round-trip; refused keys ignored or rejected, never honoured (06 §4.1, `¬`)                                                                    | V-CTR-005                                         | L1    | —                             |
| **T9b-3** | Broker supply-chain minimality: no LLM SDK, plugin loader, interpreter or shell in the SBOM; no `/bin/sh` in the image; one listening socket; two mounts (`¬`) | V-RUN-010                                         | L0    | —                             |
| **T9b-4** | `dev/verify/verify-phase9.sh`, its `dev/L0-CHAIN.txt` / `dev/L2-CHAIN.txt` lines, and the V-BRK-021 L0-vs-L2 evidence reconciliation                           | (the gate itself)                                 | L0    | T9b-1..3 landed               |
| **T9b-5** | `broker-execute-l2.sh`, `actor-grant-sweep-l2.sh`, the tenant overlay's **write** half and the admission ruling it needs                                       | V-BRK-006/018/019, V-REV-002, V-REV-003 (L2 half) | L2    | P1 — images built after T9b-4 |

T9b-5 is also what unblocks **P9-T8b-4b-ii-2b-ii** (the envelope corpus soak, journal mining, guard
3 as a label assertion per [[LSN-045]], and V-REV-001 at L2), which is why that unit sits behind it
rather than beside it.

**The denominator moves by four**, not by five: T9b was already counted once.

#### T9b-5 splits again into three: T9b-5a … T9b-5c

**Recorded 2026-07-30, at SELECT, under `harness-run` §2 sizing.** The row above bundles three
things that are separable, have different subjects, and — the deciding argument — have a strict
dependency order that the bundle hides. `broker-execute-l2.sh` cannot reach step 8 without a write
authority the actor does not have: `execute/client.go` issues real API calls with
`client.DryRunAll`, and a server-side dry-run is **authorized normally** before it is dry-run, so
the API server refuses `patch deployments` for the phase-9 actor exactly as it refuses a live one.
Every envelope in P9-T8b-4b-ii-1 died at step 3 for the read half of that same reason. Writing the
suite first and the overlay second would mean writing 700 lines that cannot run.

| Unit       | What it is                                                                                              | Checks                                           | Level |
| ---------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------ | ----- |
| **T9b-5a** | The tenant overlay's **write** half, and the admission ruling it needs — executed, not read off the CEL | (enabling; V-CTN-037 stays green)                | L2    |
| **T9b-5b** | `broker-execute-l2.sh` — Accept (a) end to end, Accept (d)'s journal half, and V-BRK-021's L2 half      | V-BRK-006/018/019/021, V-REV-002, V-REV-003 (L2) | L2    |
| **T9b-5c** | `actor-grant-sweep-l2.sh` — Accept (e)'s two-sided `auth can-i` sweep over every agent identity         | V-BRK-013 (L2 half)                              | L2    |

**5a claims no check ID, deliberately.** It is enabling work, in the same sense T9b-4 was: the
breakdown row names the checks that the thing it enables will carry, and inventing an ID for the
enabler would put a row in `results.csv` for a property nothing measured. What it does instead is
_execute the three premises the ruling rests on_, which until now existed only as a paragraph in
`dev/verify/fixtures/actor-tenant-grant.yaml`'s header reading CEL and predicting what the API
server would do with it.

**The denominator moves by two more.**

#### T9b-5b splits in two: T9b-5b-i and T9b-5b-ii

**Recorded 2026-07-30, at SELECT, under `harness-run` §2 sizing.** The row above bundles three
subjects that share only the fact that they all point at a running broker. Accept (a) is a
**positive**: submit one well-formed envelope and read back what the journal recorded. Accept (d)'s
journal half is a **fault injection**: take the journal away from a broker that is already serving
and watch it decline. V-BRK-021's L2 half is a **surface scan**: debug routes, override parameters,
the ten bypass headers, one listening port, no build-tag skip path. Three different fixtures, three
different failure modes, and only the first of them establishes the submission path the other two
need to be interesting.

| Unit          | What it is                                                                                                    | Checks                                                                       | Level |
| ------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ----- |
| **T9b-5b-i**  | `broker-execute-l2.sh` — one envelope: submit → classify → journal → shadow-execute, and the record read back | V-BRK-006 (L2 clause), V-REV-001 (n=1) — **revised at IMPLEMENT, see below** | L2    |
| **T9b-5b-ii** | The journal-unavailable refusal (Accept (d)'s half) and V-BRK-021's L2 surface scan, on the same driver       | V-BRK-021, V-BRK-018, V-REV-003, Accept (d) journal half                     | L2    |

**Why the positive goes first, and alone.** `verify-phase9.sh` section B says in its own words that
"an envelope flows end-to-end in shadow mode and produces a well-formed `ActionRecord` with a valid
undo plan" **has never been executed once against a cluster** — four suites prove the undo machinery
and not one of them submits an envelope. That sentence is the phase's largest unmeasured claim, and
it is worth landing on its own rather than as the first third of a unit whose other two thirds are
fault injection. It is also the strict prerequisite: a journal-unavailable refusal is only evidence
if the same envelope succeeds when the journal is there, and 5b-ii's fixture is 5b-i's fixture with
one thing broken.

**The denominator moves by one more.**

---

### P9-T9c — the auto-pause 06 §4.4 row 3 promises and nobody performs

**Scheduled 2026-07-31 at ORIENT from [`BACKLOG.md`](BACKLOG.md) **B-006**.** Appended to the end of
the Phase 9 ladder rather than inserted, so it displaces nothing: `5b-ii-b` → `5c` →
`T8b-4b-ii-2b-ii` → **`T9c`** → `harness-milestone`.

**What is wrong.** `broker-refuse-l2.sh` proved the journal-unavailable refusal live on 2026-07-31 —
503, `reason: journal-unavailable`, zero `ActionRecord`s over 20 s, no object applied. That is the
refusal half of 06 §4.4 row 3 and it is real. The **pause** half is not. `internal/broker/brake.go`
sets `AutoPause: true` on the row-3 decision (`:456` and `:547`) and the reply the caller receives
says _"and the agent is being paused"_ — but nothing in `internal/broker/pipeline/`, `server.go` or
`cmd/broker/` reads the field, and `status.broker.journalReachable` has no writer
(`agent_controller.go:402`). An agent whose journal has failed therefore keeps being asked and keeps
refusing, one submission at a time, with no fleet-level signal that it has gone dark.

**Why this is a gap and not unbuilt scope.** Row 9's auto-pause **is** wired —
`internal/broker/verify/driver.go:400` calls `escalate.Recorder.Pause`. The mechanism exists and one
of its two callers was never connected.

**What to build.** Consume `AutoPause` on the row-3 path through the same `escalate.Recorder.Pause`
seam row 9 uses, and give `status.broker.journalReachable` a writer. Do not change the refusal: the
503 and its `retryAfterSeconds` are proven and are what keeps this fail-closed today.

**Split into `-1` and `-2` at IMPLEMENT on 2026-07-31** (`harness-run` §2 sizing). The two halves
looked like one task and are not: the pause has a seam (`escalate.Recorder`, already built and
already driven by row 9) and needed wiring, while `journalReachable` has no transport at all. The
broker cannot write `Agent` status — 06 §2.2.1 gives it `get, list, watch` on `agents` — and the one
principal that can, `agent_controller`, rebuilds `BrokerStatus` from scratch on every reconcile and
says in its own comment that it cannot observe the value. That is a design question, not a wiring
job, and pinning it to the same unit as the wiring would have bought a worse answer to it.

- **`-1` — the row-3 auto-pause consumer.** Done, below.
- **`-2` — a writer for `status.broker.journalReachable`.** The open question, stated so `-2` does
  not rediscover it: what carries "the journal is unreachable" from the only process that can
  observe it to the only principal that can write it, given that the journal itself is the surface
  that is down? A broker answering "yes" over HTTP proves nothing about its own writes (the
  controller's comment); a broker answering "no" is an admission against interest and is credible.
  A fail-closed zero and a probe that can only lower the value is one shape worth costing.

**Verification is bound at the improvement pass, not here.** 09 has no check ID covering row 3's
pause. Adding one edits the conformance spec, which `harness-improve` §5 makes a pass's work rather
than a unit's, so this task is written against a spec sentence (06 §4.4 row 3) and B-006's check
question travels separately. Recorded here so that P9-T9c's PLAN does not rediscover it.

**Severity, classified at the drain: not a live security regression.** The absent behaviour fails
open in availability terms and closed in safety terms — nothing executes, every submission is
already refused. What is lost is observability and the truthfulness of the caller-facing message. It
lands in Phase 9 rather than Phase 10 because the brake is P9-T6 and this phase's whole thesis is
landing the safety machinery while the worst possible bug is still a no-op.

---

### P9-T9b-1 — outcome, 2026-07-30

**V-CTR-006** — "`ActionRecord` lifecycle: every legal transition succeeds, every illegal one is
rejected (06 §4.3)". The check was scheduled as test-writing over an existing rule. There was no
rule. Two defects in shipped code, both found by trying to write the check:

1. **Nothing enforced the lifecycle anywhere.** `journal.Store.SetPhase` accepted `Verified →
Pending`, `Rejected → Executing`, and `"" → Undone`. The lifecycle existed as an ASCII diagram in
   a doc comment on `ActionPhase`, which is a statement of intent rather than a property of the
   system. 06 §4.3's status-RBAC table then rests on that lifecycle — the ChatOps gateway is
   permitted `PendingApproval → Pending/Rejected` **and nothing else**, the undo controller `→
Undone` **only** — so both rows were unenforceable in the direction admission cannot cover: not
   "who may write the field" but "what may the field become".
2. **`status.phase` was never populated at creation.** `status` is a subresource, so `client.Create`
   sent the block and the API server dropped it; only `Labels[kube-agents/status]` landed. Every
   record read back `status.phase: ""` while its label named a phase — the exact inversion of 06
   §4.3, which makes the field authoritative and the label a derived index. A parked record
   therefore had no `PendingApproval` for the gateway's one permitted transition to leave from.
   `rejection.go:156-158` carries a comment asserting "the status subresource is set by the
   reconciler"; no such reconciler exists.

**The ruling the table needed, which is not a halt.** 06 §4.3's diagram draws `Failed ──▶
RolledBack`. The same section's phase table marks `Failed` terminal, and `verify/driver.go`
implements the table: 04 §5.1 rung 3 succeeding writes `RolledBack`, rung 5 (rollback itself failed)
writes `Failed` and pages. Rather than pick between a picture and a column, the edge was settled
from the spec's own **principal list**: the four writers of `status.phase` are the owning broker,
the undo controller (`→ Undone` only), the ChatOps gateway, and the exporter (which deliberately
cannot touch `phase`). **No principal can write `Failed → RolledBack`.** That is an
invariant-preserving resolution derived from the finer of two statements in the same section, so it
is a decision, not a §8.5 contradiction. `Verified → Undone` survives the same test for the opposite
reason: "terminal" is a claim about the broker's pipeline stopping, and `Undone` is a different
principal, later. Both arguments live in `actionrecord_phases.go`'s file comment and in the ledger's
decisions table.

**The check is a closed truth table, not a list of remembered edges.** 121 ordered pairs (ten phases
plus the empty from-phase), with the expected answer transcribed from 06 §4.3 a **second** time
rather than read out of the production map — a test that iterates the map to decide what to expect
asserts that the map equals itself, and stays green through deleting every entry. Vacuity guards pin
27 legal cells and 94 refused. Alongside it: the CRD enum is read out of `actionrecord_types.go` as
data and cross-joined against the table in both directions; reachability is a real BFS from the
creation set, so orphaning a phase fails; and `Successors()` is asserted to hand back a copy.

**The escape the sweep found, and what it says about the fake client.** The first sweep ran 11/12.
`M10` — restoring defect (2) exactly — survived. The reason is that **controller-runtime's fake
client does not model the status subresource on `Create`**: `withStatusSubresource` is consulted in
`tracker.update` and not in `fakeClient.Create`, so the fake keeps a status block that every real
API server discards. The test asserting `status.phase` came back was green because the fake never
dropped it, and would have stayed green against a `Create` that wrote no status at all. That is the
mechanism by which the defect survived five phases under a green suite. Fixed at the helper rather
than in the one test that noticed: `newFakeStore` now installs a `Create` interceptor that zeroes
`Status` before delegating, so the whole package is measured against the cluster it will run on.
Second sweep: **12/12 caught**.

**Findings filed, not fixed.** (a) The ASCII lifecycle diagram in `06-api-and-data-contracts.md`,
reproduced verbatim in `actionrecord_types.go`, still draws `Failed ──▶ RolledBack` and still says
DryRun is "reached from Pending" — a spec-art correction for the next improvement pass, not a
behaviour change. (b) `rejection.go:156-158`'s "the status subresource is set by the reconciler" is
now moot for `phase` and the sentence is still wrong. (c) A candidate gate rule with a wider blast
radius than either: **a fake-client helper that models a subresource on `Update` and not on
`Create` is a suite that cannot see its own most likely defect** — every `WithStatusSubresource`
call site in this repository is a candidate, and none of the others has been audited.

Evidence: **V-CTR-006 (L1) pass** — `verification/results.csv`, `verification/mutants/V-CTR-006.json`
at 12/12.

---

### P9-T9b-2 — outcome, 2026-07-30

**V-CTR-005 — "Envelope schema round-trip; refused keys are ignored or rejected, never honoured"
(06 §4.1, L1, `¬`) — recorded at L1 for the first time.** New:
`k8s-operator/internal/broker/envelope_roundtrip_test.go`, two valid fixtures, and
`verification/mutants/V-CTR-005.json` at **13/13 caught**.

**No defect in shipped code this time. The defect was in what the corpus could see.** The round-trip
property itself — decode → marshal → decode — already held over every valid fixture and was simply
unasserted. What was not true is the thing an unasserted round-trip is usually assumed to imply:
that the corpus exercises the schema. Reflecting over `Envelope` yields **57 declared JSON paths**,
and **12 of them appeared in no valid fixture at all**:

| Uncovered path                                                                       | Why it mattered                                                                                             |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `dryRun`                                                                             | It is inside the idempotency key (`keyInput`). Every dry-run key in the product was unpinned on both sides. |
| `operations.cloudTarget` + `.provider` `.service` `.resource` `.method`              | The entire non-Kubernetes target shape, including its own leg of `operationSortKey`.                        |
| `operations.delete.gracePeriodSeconds`, `.preconditions` + `.uid` `.resourceVersion` | The "delete this object, not the one that replaced it" guard.                                               |
| `requester.assertion`                                                                | The field whose absence is what makes a record `attributionUnverified`.                                     |
| `trace.threadId`                                                                     | Correlation back to a chat thread.                                                                          |

Every one of those is legal per `Operation.validate`, handled by the Python builder's `_CLOUD_FIELDS`
/ `_DELETE_FIELDS` / `_PRECONDITION_FIELDS` projections, and reachable from the MCP tool — they were
simply never written down in an artifact anything asserts against. A decoder that dropped any of
them would have left `TestFixtureCorpus`, `TestValidFixtureIdempotencyKeys` and the whole V-BRK-028
Python↔Go join green.

**So the first test in the file is a coverage assertion, not a round-trip.**
`TestEveryDeclaredSchemaPathIsExercisedBySomeValidFixture` walks the Go type for declared paths,
walks the corpus for observed ones, and fails on the difference. It is the vacuity guard the
round-trip needs, and it is self-maintaining in the direction that matters: **adding a field to the
wire schema now fails until some fixture carries it.** Path walking stops at `desiredState` and
`patch.body` in both directions — they are deliberately open (`TestPatchBodyTypeIsNotClosed`), and
without the stop a `desiredState` containing a key named `scale` would read as coverage of
`operations.scale`.

Two fixtures close the twelve:

- **`platform.cloud-nodepool-resize.json`** — a `scale` op against a GKE node pool `cloudTarget`,
  under `dryRun: true`. Its key had to be computed fresh, because `dryRun` is in `keyInput`.
- **`cluster-admin.delete-with-preconditions.json`** — a single-object `delete` carrying
  `propagationPolicy`, `gracePeriodSeconds` and both preconditions, from a human requester with a
  router-signed `assertion` and a `trace.threadId`.

Both were added to `identities.json`, so they join the Python↔Go idempotency comparison
automatically; `dev/test_action_envelope.py` went from six shapes to eight with no edit to the test.
The four docstrings that said "the same six envelopes" now name no count at all and point at
V-CTR-005 as the reason the corpus grows — **a count in prose is stale by construction**, which is
the same failure mode as the "next free ID" lines already filed in this file.

**The other half — "never honoured".** `TestFixtureCorpus` already proves the reserved-key refusal
fires. "Never honoured" is a stronger claim: that the value could not reach a decision even if the
scan were removed. Two mechanisms, asserted separately:

1. **No field of `Envelope` is spelled with a reserved name.** Today the scan runs before strict
   decoding so such a field would be unreachable — but that ordering is a two-line edit, and every
   other test in the package would survive reversing it (M10).
2. **Every reserved name is also an unknown field to a bare strict decoder.** Strip the scan and the
   closed schema still refuses all fourteen, losing the security event and never honouring the
   value.

**And the list is joined to the spec.** `TestTheReservedKeyListIsTheOneTheSpecPublishes` parses
06 §4.1's "What the broker ignores — and what it refuses" table as data and compares rows 1–4 to
`ReservedKeys` in both directions, per [[LSN-040]]/[[LSN-041]] — two definition sites of a security
rule are only allowed here when something mechanically compares them. The table is parsed
positionally, so its **shape** is asserted before its contents: eight data rows, row 5 the
anti-replay row, row 6 the closed-schema catch-all. A reordered table fails loudly rather than
silently redefining which rows are the reserved ones. `bypassFamily` is asserted as a **superset**
of row 3, not an equal: the code also puts `approved` and `undoPlan` in it, a widening the row
boundaries do not express and the prose does support.

**The sweep is the reason to believe any of it.** 13/13, and three of the mutants exist only because
a test that reads prose as data is one silent parse failure away from asserting nothing (LSN-048).
**M12 renames the spec heading by two characters** and must turn the suite red; **M13 deletes
`severity` from the published table while the broker goes on refusing it** — the drift that actually
happens, invisible from the code side. **M5 and M6 add an undocumented field** to `Envelope` and to
`CloudTarget`: they are not defects in the guard, they are the defect the guard is _for_.

Findings filed, not fixed:

- **`TestFixtureCorpus`'s doc comment claims to be V-CTR-005** and is not — it is the corpus decode
  and the V-BRK-002 refusal half. The round-trip and coverage halves did not exist until now. That
  is the **fifth** wrong or over-claiming `V-*` binding filed in this phase, after V-BRK-020's
  citation, `execute/apply.go` citing V-REV-002 for V-BRK-006, T8b-3's row binding V-GAT-019, and
  V-BRK-008 ≡ V-BRK-017. **A class, and overdue for the improvement pass**: a candidate gate rule
  that every `V-*` mentioned in a Go or Python doc comment names a check whose 09 §6 statement the
  test actually asserts.
- The reserved-key table's **rows 7 and 8** ("recorded and ignored", "recorded, not trusted") are
  not joined to anything. `rationale` never reaching the classifier is a V-BRK-surface property and
  `attributionUnverified` is a journal one; neither is this check's, and neither is asserted from
  the table.

Evidence: **V-CTR-005 (L1) pass** — `verification/results.csv`, `verification/mutants/V-CTR-005.json`
at 13/13.

---

### P9-T9b-3 — outcome, 2026-07-30

**V-RUN-010 — "Broker supply-chain minimality" (08 §2.1, §2.6, L0, `¬`) — recorded for the first
time.** New: `dev/tests/broker-supply-chain-minimal.py`, two lines in `dev/L0-CHAIN.txt`, negative
control **15/15**.

**The unit found three real defects, all of them shipped, all of them invisible to the review that
would normally be trusted for this property.** Writing the check is what found them; none was
suspected going in.

1. **`os/exec` was in the broker binary via first-party code.** `internal/journal/auditsource.go`
   held `CloudLoggingSource`, which runs `exec.CommandContext(ctx, c.bin(), "logging", "read", …)`
   where `c.bin()` defaults to `"gcloud"` and is a settable struct field. `internal/journal` is
   linked into `cmd/broker` both directly and through `internal/broker`. It was unreachable —
   nothing outside its own tests constructs one — which is precisely why nothing noticed, and it was
   one call site from reachable inside the only process in the mesh whose ServiceAccount can write.
   Fixed by extracting it to **`internal/journal/cloudaudit`**, a package whose consumer is the
   V-BRK-003 reconciler in the operator and never the broker. The package boundary is the
   enforcement; the doc comment says so, and `var _ journal.AuditSource = (*Source)(nil)` sits at
   the boundary so the split implementation cannot drift from the interface silently.
2. **A plugin loader was registered on purpose.** `cmd/broker/main.go` carried kubebuilder's
   scaffolded `_ "k8s.io/client-go/plugin/pkg/client/auth"`. That import's entire job is to register
   out-of-process credential providers so a kubeconfig may name a binary for the client to fork. The
   broker authenticates exactly one way, with the projected token the kubelet mounts. Deleted.
3. **`net/http/pprof` was linked in, and the file's own doc comment said it was not.**
   `ctrl "sigs.k8s.io/controller-runtime"` is a facade over `pkg/manager`, `pkg/builder` and
   `pkg/controller` — an entire controller runtime in a process that runs no controller — and
   `pkg/manager` imports `net/http/pprof`, whose package init registers `/debug/pprof` on
   `http.DefaultServeMux`. main.go's package doc has claimed "No metrics listener, no pprof, no
   admin socket, no second Service" since the file was written. Replaced with the four narrow
   subpackages (`pkg/client`, `pkg/client/config`, `pkg/log`, `pkg/log/zap`, `pkg/manager/signals`),
   four call-site renames, no behaviour change. `go list -deps ./cmd/broker` went from
   `{os/exec, runtime/pprof, net/http/pprof}` to `{os/exec}`.

**Why the import graph is the load-bearing property and the image is not.** The broker image was
already `gcr.io/distroless/static:nonroot` — no shell, no package manager, non-root, read-only root.
Every one of the three defects survived that, an SBOM diff and an RBAC review, because a Go binary's
shell is `os/exec`, not `/bin/sh`: the image can be shell-free while the process retains the ability
to fork one, and that is the half an image scan structurally cannot see. So P1 walks the first-party
import graph textually from `cmd/broker` (23 packages today, with a floor and three must-reach
packages as the [[LSN-035]] non-vacuity arm) and forbids shells, plugin loaders, interpreters,
inference clients and debug HTTP surfaces in **any** of them. Textually because L0 CI has Python
3.12 and no Go toolchain by design; a graph walk rather than a scan of the entry package because all
three defects were a package or more away from `main`.

**Stated non-claim, recorded rather than waved away.** `k8s.io/client-go/rest` imports
`plugin/pkg/client/auth/exec` unconditionally, and `k8s.io/apiserver` brings
`github.com/google/cel-go`. Both are in the indirect closure of having a Kubernetes client at all
and no import discipline in this repository removes either. P2 therefore scopes to **direct**
requires — the set this repository actually chooses — and the residue is named in the check's
docstring and moved to an L2 SBOM scan of the built image, which is where a module graph exists.
P1 still catches the case that matters: a `cel-go` import in `internal/broker` fails on the line it
is written.

**The negative control earned its place twice, against the check rather than the code.**
(a) The base allowlist matched by prefix, so `gcr.io/distroless/static:debug` passed — same
repository, plus a busybox shell at `/busybox/sh`, and the exact tag someone reaches for at 3am
trying to get a prompt inside the broker. Now matched on repository exactly, with a separate tag
scan. (b) The `Volumes:` extractor was a lazy regex terminating on `\n\t+\},\n\t+\}`, which matched
the end of the nested `SecretVolumeSource` instead of the end of the slice; P6 read only the first
volume and would have gone on passing had the two been declared the other way round. Replaced with a
brace-depth matcher. Nested Go composite literals are not a regular language.

**Filed, not fixed** — for P9-T9b-4 or the next improvement pass:

- **08 §2.6 says two mounts and the renderer makes three.** The third is a `/tmp` `emptyDir` whose
  stated justification ("Go's TLS stack and the client-go transport both want a temp dir") is
  unverified and only testable against a running broker. It is carried as the one named, reasoned
  entry in `ALLOWED_MOUNTS`, and pinned to **be** an anonymous `EmptyDir` so the same mount name
  cannot quietly become a Secret, ConfigMap, hostPath or PVC. The spec sentence is deliberately not
  edited here: PROTOCOL §10 forbids resolving a check's own failure by editing the thing it checks
  in the same unit. Resolution is either an L2 probe that removes the mount and watches the broker,
  or a sentence in 08 §2.6 recording the ephemeral scratch.
- **A doc comment asserted a security property the build contradicted** (defect 3 above). This is
  another instance of the class already carried from T9b-1 and T9b-2 — the candidate gate rule that
  every `V-*` or posture claim in a Go or Python doc comment must name something mechanically
  asserted. It is now the sixth instance in this phase, and the first where the claim was not a
  `V-*` ID but a plain-English list of what the binary does not contain.

Evidence: **V-RUN-010 (L0) pass** — `verification/results.csv`; the check replayed against
`git show HEAD:` content for `cmd/broker/main.go` and `internal/journal/auditsource.go` with the new
`cloudaudit` package removed reports all three shipped defects, which is the evidence that it would
have caught them.

---

### P9-T9b-4 — outcome, 2026-07-30

Three deliverables: `dev/verify/verify-phase9.sh`, its chain lines, and the V-BRK-021 L0-vs-L2
reconciliation. All three landed. Two of them turned up something the unit was not looking for.

**The gate itself.** `verify-phase9.sh` follows `verify-phase8.sh` exactly where the template is
load-bearing — no default target, an anchored `gke-scratch-*` `case`, P10 first as a hard `exit 2`
(could-not-run, never 1), P1 with the three-state `dev_ok`, section A running `dev/L0-CHAIN.txt` **as
a file** with a shrink guard, one `run_l2` as the only place a sub-suite's rc is interpreted
(`0` pass · `3` defer, never a pass · `2` could-not-run · `*` fail), and a final section that prints
deferrals without asserting them. Sections A–I bind to Accept (a)–(e), the ratchet and the
deferrals. Two departures, both argued in the file: the three-state P1 read is a function
(`p1_gated`) rather than a block copied into each section, because Phase 9 has six cluster-bound
sections to Phase 8's four and the copies had already drifted in wording; and `cd "$REPO_ROOT"`
carries `|| exit 2`, because section A runs 43 commands relative to it and a failed `cd` would
produce 43 wrong answers rather than one.

**Five artifact arms, four of them red today, and that is the deliverable.** Accept (a)
(`broker-execute-l2.sh`),
Accept (d)'s journal half (same artifact), Accept (e) (`actor-grant-sweep-l2.sh`), V-BRK-021's L2
claimant, and planning defect 2's guard 1 are all detected **by artifact** — a file that must exist,
a check function that must be registered, an ID that must be claimed by a script that is in the
chain. None of them is a comment saying "T9b-5 pending". The gate therefore goes green on its own
when the work lands and cannot be talked into it before, which is the one property of
`verify-phase8.sh` section E worth copying. The shape is worth stating once: **Accept (a) — "an
envelope flows end-to-end in shadow mode and produces a well-formed `ActionRecord` with a valid undo
plan" — has never been executed once against a cluster.** Four suites prove the undo machinery (the
index resolves, the prober reads back, the replayer restores) and not one of them submits an
envelope.

**The fifth artifact arm went green, and the first version of it was right by accident.** Section G
also looks for planning defect 2's guard 1, which the 2026-07-29 recon recorded as not existing. It
does now — `check_test_only_grants_are_confined` (V-CTN-037) landed the same day under
P9-T8b-4b-ii-2a — so the recon bullet is annotated closed above. The detector, though, was written to
match a **function name** containing `test_only` or `overlay`, and it passed on the first run because
the name and the truth happened to coincide. Telling those two apart is the entire job of section G,
so the arm was rewritten to assert the property: a check whose body references the marker
`kube-agents/test-only-grant` — **resolved through the constant, not accepting its spelling** — that
is registered in the gate's `CHECKS` table, in a gate that is a live line of `L0-CHAIN.txt`. Six
mutants against it: de-register the check, delete it, hollow the marker constant's value, drop the
gate from the chain, comment the gate's chain line out — all five caught. The sixth ("the body stops
using the marker") scored **BROKEN**, not escaped: five other references to the constant survived the
edit, so the mutant never made the change it claimed to (LSN-048). The intermediate version, which
also accepted the bare identifier `TEST_ONLY_MARKER`, let the hollowed-constant mutant through — a
name match one level up from the one it replaced.

**The L2-CHAIN decision, and the drift it exposed.** The standing regression line said
`verify-phase7.sh`. Phase 8 closed on 2026-07-27. So for three phases the standing baseline was a
phase behind, and the six Phase-8 suites were run as Phase 8's own evidence rather than as the floor
every later phase clears — the same commands, a different claim. That is the visible half. The
invisible half is why this is recorded rather than just fixed: `invariants-gate.py` derives the set
of scripts it lints for declared preconditions from the **transitive closure of `L2-CHAIN.txt`**
(`_l2_scripts_in_scope`). A phase gate in no chain line, called by nothing, is outside that closure.
**`verify-phase8.sh` — the single script that renders Phase 8's verdict — was never once asked which
artifact it was judging.** It happens to declare its preconditions correctly; nothing checked, and
the only way to discover that was to ask why the standing line was stale.

Resolution: the standing line advances to `verify-phase8.sh`, and `verify-phase9.sh` is added under
its own heading **while the phase is open and while it is red**. The argument for listing a red gate
is that a gate added only once it passes has never told anyone something they did not already know;
the artifact arms are worth having in an L2 run now, not at the milestone. At Phase 9's milestone it
becomes the standing line and the seven Phase-9 lines collapse into it.

**The correction that came from running the gate: a check I was about to call coarse was right.**
Advancing the standing line and dropping the `verify-phase7.sh` line turned
`check_closed_lessons_are_executable` red within one run — LSN-013 and LSN-028 are both closed
naming `verify-phase7.sh` as their mechanizing artifact, and that check resolves chain lines
**literally**, unlike `_l2_scripts_in_scope` four hundred lines away in the same file. The first
instinct was that the two resolvers should agree and that this one should follow delegation. It
should not. `verify-phase8.sh` runs `verify-phase7.sh` only when P1 resolves, so a delegated call is
a **conditional** one; "named by a chain line" means "runs every time the chain runs", which is
strictly stronger than "is reachable". A lesson mechanized by an artifact that runs only when a
precondition happens to hold is a lesson that can stop being mechanized on a cluster with a stale
image, silently. So `verify-phase7.sh` keeps its own line, and the same argument is now written
above the Phase-8 block, which is also conditionally reached and also stays listed.

**Two floors raised, in the commit that moved the chain, because their own docstrings say to.**
`L2_CHAIN_FLOOR` sat at **6** against a 14-line chain and `L2_SCOPE_FLOOR` at **16** against a
23-script closure. Both comments say in as many words that a floor below the real count tolerates
exactly the change it exists to notice; neither had been moved since it was written. Now 16 and 25.
This is a strengthening, not a check edit motivated by a failing implementation — the checks were
green before and after — and the milestone that collapses Phase 9's lines will have to lower
`L2_CHAIN_FLOOR` deliberately, which is the point of the number being there.

**The V-BRK-021 reconciliation.** The 2026-07-29 recon bullet read "V-BRK-021 needs both L0 and L2;
its only evidence is L1". Both halves are now false and the bullet is annotated superseded in place
rather than rewritten, because it was accurate when written.

| Level | 09 §6 requires it | Evidence on file                                                     |
| ----- | ----------------- | -------------------------------------------------------------------- |
| L0    | **yes**           | `results.csv` row 138, 2026-07-30, P9-T7c-2c — **pass**, sweep 10/10 |
| L1    | no                | `results.csv` row 54, 2026-07-27, P9-T2 — pass                       |
| L2    | **yes**           | **none**                                                             |

So the L1 row was never load-bearing: it neither discharged the requirement nor constituted the gap,
and reading it as "the only evidence" made an unrequired level look like partial credit toward two
required ones. The gap is the **L2 half and only that**. It is also not, as the bullet said, "a lint
over the shipped image" — a lint is what the L0 half already is, and the L0 half is a source
derivation (`MutatingRoutes()` equals `Registered()` less a declared allowlist, one registration call
site, every mutating route reaching the authenticator and the pipeline over the call graph). The L2
half is the clause a source scan cannot reach: debug routes, override query params and the ten
`X-Kube-Agents-*` bypass headers all 404/405 against a **running** broker, exactly one listening port
on the pod, and no build-tag-guarded skip path in the image the controller actually handed out. A
lint proves what the tree says; only a probe proves what was shipped. V-BRK is BLOCKING-ALWAYS, so
this may not be deferred — section G of the gate is red until an `*-l2.sh` claims the ID **and** that
script is a live line of `L2-CHAIN.txt`. Both conjuncts matter: a script claiming the ID from outside
the chain is evidence nobody gathers.

**Filed, not fixed.**

1. `check_closed_lessons_are_executable` and `_l2_scripts_in_scope` both answer "is this script run
   by the chain" and give different answers. Both are right for their own purpose, and nothing says
   so — a future reader will reasonably conclude one is a bug. Candidate: name the two properties
   apart in the source (`run_unconditionally` vs `reachable`) so the divergence is a decision rather
   than an accident.
2. The Phase-8 suites are now run twice per L2 chain pass — once through the standing line
   conditionally, once as their own unconditional lines. Accepted deliberately (the conditional
   caller argument above), but it is real cluster time, and the same duplication is about to double
   for Phase 9. Candidate for the milestone: a gate that takes `--already-regressed` rather than a
   chain that runs the same suite twice.
3. **The candidate gate rule this unit most wants mechanized:** _the standing regression line of
   `dev/L2-CHAIN.txt` names the most recently closed phase's gate._ It is derivable — the ledger
   knows the last closed phase, `dev/verify/verify-phase<N>.sh` is a naming convention the repo
   already relies on, and the drift went three phases unnoticed. This is `harness-improve`'s, not
   this unit's.

---

### P9-T8b-4 splits: 4a is the deployment path, 4b is the soak

**Recorded 2026-07-30, at SELECT.** T8b-4 is "the L2 shadow soak with journal mining". Surveying
what the soak needs before starting it turned up a defect in the shipped system, not a gap in the
test scaffolding, and the defect has to be fixed before any L2 broker claim can be made at all:

- `pod_launcher.go:168` renders a **broker Deployment for every `Agent` CR**, and the `PodLauncher`
  interface deliberately offers no way to ask for just the agent half.
- The broker's image comes from `brokerImage()`, which reads `KUBEAGENTS_BROKER_IMAGE` off the
  controller's own Deployment and otherwise falls back to
  `ghcr.io/gke-labs/kube-agents/kage-broker:v0.1.0`.
- **`KUBEAGENTS_BROKER_IMAGE` is set nowhere in this repository** — not in `config/manager/`, not in
  the provisioning path, not in `reload-images.sh`. Checked, not assumed.
- That GHCR tag is one of the four the **V-CMP-002** deferral measured as unpullable (the other
  three answer 403; `platform-agent` answers 404).

So every `Agent` CR on every cluster renders a broker Deployment whose pod cannot pull. That is 09
§11.9 — built, never wired — in the component that holds the actor credential, and
`reload-images.sh` says so about itself in its own header: the `broker` target "repoints NOTHING …
Once P9-T7 lands this grows a `deploy_broker`". P9-T7 landed four units ago. The note aged into a
defect.

**The split.** 4a is the deployment path and the L2 claim that path makes true; 4b is the soak.

| Unit          | What                                                                                                                                                                                     | Checks             | Blocked on                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------ |
| **P9-T8b-4a** | `deploy_broker`; the `kage-broker` P1 mapping; the actor identity as a dev fixture rendered by the shipped renderer; `dev/verify/broker-per-agent-l2.sh` and its `dev/L2-CHAIN.txt` line | **V-BRK-012 (L2)** | a live scratch cluster               |
| **P9-T8b-4b** | The shadow soak proper: drive envelopes at the deployed broker and mine the journal — **split again into 4b-i and 4b-ii below**                                                          | V-REV-001 (L2)     | T8b-4a — nothing to drive until then |

**Why V-BRK-012 and not a new ID.** 09 §6.2 gives V-BRK-012 as `L0, L2` and `verification/results.csv`
records only the L0 row (2026-07-28, P9-T7b), whose own note ends "the lint reads source, so it says
nothing about a _deployed_ fleet — that is the `L2` half, P9-T9's". The L2 half is the open half, it
is BLOCKING-ALWAYS with a mandatory `¬`, and it is exactly the claim a working deployment path makes
checkable: one broker per CR, owned by that CR, on the digest under test, with a Service whose
endpoints resolve to its own broker pod and nobody else's. Adding an ID for "the broker deploys"
would be a second name for the same property (V-MET-013), so the row moves from T9b to here.

**LSN-015 is honoured by the fixture, not by a note.** The two shipped manifests
`examples/gitops-repo/fleet/platform-agent.yaml` and
`clusters/cluster-a/agents/agent.yaml` both live in `kubeagents-system` — a platform broker and a
cluster-admin broker co-located, which is 08 §2.6's shape and the only arrangement in which "the
Service selector pins `agent:` as well as `role:`" can fail. A one-CR fixture cannot fail it. They
are seeded through `seed_parent_agent`, so they are the shipped manifests and not this suite's
paraphrase of them (LSN-024).

**Three consequences for P1, all of which are work in 4a.**

1. `_p1_build_inputs` maps `k8s-operator` and `kage-router` and **returns 1 for everything else**,
   so P1 against a broker pod answers state 3 — could not verify — and a broker suite that mapped 3
   to a pass would be certifying whatever image happened to be running. `kage-broker` builds from
   the same `k8s-operator/` context and gains the mapping here.
2. The freshness half compares the deployed tag against `git rev-parse --short HEAD`, so the tree
   must be committed before the Cloud Build and must not move until the L2 run is over. Same
   discipline as T9a's ordering argument, one level tighter: this unit's own ledger commit is taken
   **after** the run, not between the build and it.
3. The broker image reaches the pod through the **controller's** environment, so P1 has two subjects
   here and both are asserted: the controller pod (which chose the image) and the broker pod (which
   is running it). A cluster where those two disagree is one where the rendered Deployment is a
   generation behind the env var, and every claim below it would be about the previous build.

---

### P9-T8b-4b splits again: 4b-i is a caller that can get in, 4b-ii is the soak

**Recorded 2026-07-30, at SELECT.** 4a made the broker _exist_ on a cluster. Sizing 4b — "drive
envelopes at the deployed broker and mine the journal" — turned up that the sentence hides two
different units, and the first one is not the soak:

- **Nothing in `dev/` can speak to a broker.** Not the shell, not a probe, not a fixture. The broker
  requires mutual TLS with a certificate this cluster's `kubeagents-mesh-ca` signed, a projected
  ServiceAccount token carrying audience `kubeagents-broker`, and the two bound to each other
  through a SPIFFE URI. `broker-per-agent-l2.sh` deliberately said so: it proves each broker pod
  _runs_, and states as a non-claim that it does not prove the broker _serves_, "because no client
  here holds a certificate".
- **The obvious shortcut does not work, and the reason is a finding.** Driving the shipped
  `broker_client.py` from the working tree over `kubectl port-forward` looked viable because the
  module's own docstring says "The server certificate is verified against `KUBEAGENTS_BROKER_SAN`,
  not against the host in the URL." It is not. `cfg.san` is read from the environment, required by
  `BrokerConfig.require()`, and then **never used again** — `build_ssl_context` sets
  `check_hostname = True` and `urllib` derives `server_hostname` from the URL, so the name actually
  verified is the endpoint's host. The two strings are equal today, so nothing is broken; the
  docstring describes an intent no code implements. Filed as a finding, not fixed here — fixing it
  is a change to shipped agent code and belongs in its own unit.
- So the caller has to be **in the cluster**, which is the honest arrangement anyway: it goes
  through the real Service, the real `<agent>-broker-ingress` NetworkPolicy and the real
  `<agent>-to-broker` egress hop, none of which a port-forward touches.

**What that unlocks is bigger than the soak.** Five checks in 09 §6.2 are the transport, every one
of them `L2`, phase 9, BLOCKING-ALWAYS, and carrying the mandatory `¬` — and **not one has a single
row in `verification/results.csv`**:

| ID            | Property                                                                                  | State before 4b-i |
| ------------- | ----------------------------------------------------------------------------------------- | ----------------- |
| **V-BRK-007** | mTLS is required — a plaintext or wrong-CA client is refused ¬                            | no evidence       |
| **V-BRK-008** | The projected token must carry audience `kubeagents-broker` ¬                             | no evidence       |
| **V-BRK-009** | Neither layer alone suffices ¬                                                            | no evidence       |
| **V-BRK-010** | A foreign agent's reader SA is refused **and raises a security event** ¬                  | no evidence       |
| **V-BRK-017** | The default-audience token is refused — `TokenReview` says `authenticated: true` for it ¬ | no evidence       |

They are the whole of acceptance bullet (c)'s L2 half, they were scheduled onto P9-T2 and never
gathered, and every one of them is a question about a **credential presented to a running broker**.
A driver that can present one answers all five; the soak needs the same driver and nothing more.

**The split.**

| Unit             | What                                                                                                               | Checks                                                                | Blocked on |
| ---------------- | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- | ---------- |
| **P9-T8b-4b-i**  | The in-cluster envelope driver — a real reader identity at the broker's door — and `dev/verify/broker-auth-l2.sh`  | **V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017**, all L2 | T8b-4a     |
| **P9-T8b-4b-ii** | The shadow soak proper: a corpus of envelopes through the driver, then journal mining over the `DryRun` population | **V-REV-001 (L2)**                                                    | 4b-i       |

> **Split again — see "P9-T8b-4b-ii splits" below.** 4b-ii-1 types step 3's refusals (V-BRK-031);
> 4b-ii-2 is the soak, which needs the read-only tenant overlay before its population is non-empty.

**Why the driver is a pod and what that costs.** The three agent images in Artifact Registry are
stale (2026-07-24 to 2026-07-27) and build `FROM nousresearch/hermes-agent`, so rebuilding them to
get a Python interpreter next to the shipped scripts is a slow build for a fixture. Instead the pod
runs a stock `python:3.12-slim` and the **shipped** `broker_client.py` and `action_envelope.py`
arrive in a ConfigMap generated from the working tree — so the code under test is byte-for-byte the
file this repo ships, and only the interpreter around it is a fixture. Its environment is read off
the **rendered agent Deployment** (P6), never reconstructed from the naming functions, so a driver
pointed at the wrong endpoint is a driver that fails rather than one that quietly proves nothing.

**Name resolution is short-circuited, deliberately, and it is a non-claim.** The pod carries
`hostAliases` mapping the broker's SAN to the broker Service's ClusterIP. `<agent>-to-broker` makes
every reader-labelled pod default-deny on egress and opens exactly one hole — TCP 8443 to the actor
half of its own pair — with no DNS rule anywhere in it (the real agent pod gets DNS from the
per-tier egress policy, which is an install-time artifact this cluster does not carry). Resolving
the name locally means the driver reaches the broker through **precisely** the allowance the pair
policy grants and nothing else, which is a sharper demonstration than a working DNS lookup would
be. What it does not demonstrate is that cluster DNS publishes that name; that is
`broker-per-agent-l2.sh`'s L2-3, which reads the Endpoints the API server computed.

**A second finding, filed while writing the probe: `session_trace()` can build an envelope the
broker must refuse.** `broker_client.py:367` adds `parentSpanId` to the trace whenever `SPAN_ID` is
set in the agent's environment. `broker.Trace` has `traceId`, `spanId`, `sessionId` and `threadId`
and no such field, and `DecodeEnvelope` runs `dec.DisallowUnknownFields()` — so an agent in a traced
session has **every** mutation refused with a 400 `unknown-field`, and the refusal names a trace
field rather than anything the agent did. Nothing in the repo sets `SPAN_ID` today, so it is latent;
it is also exactly the shape of defect that only appears once tracing is wired, at which point it
looks like the broker rejecting everything. Not fixed here: it is one line in shipped agent code
that all three tiers carry byte-identically, so the fix is a `dev/test_agent_script_parity.py`-scoped
edit plus a test asserting the client's trace keys are a subset of `broker.Trace`'s JSON tags — the
mirror image of the `RESPONSE_FIELDS` assertion `dev/test_broker_client.py` already makes about the
reply, which is a check that exists in one direction only and is why this was never caught.
**Scheduled as P9-T8b-4c**, with its check ID to be assigned by that unit against 09 §6.2 rather
than guessed here. The driver pod leaves
`SPAN_ID` unset and says so in a comment, because setting it would be measuring the bug from the
fixture that discovered it.

### P9-T8b-4b-i — outcome, 2026-07-30

**Green: 14 PASS / 0 FAIL, rc 0, three consecutive runs.** All five rows now have their first
`verification/results.csv` entry. The `¬` is `broker-auth-l2.sh --negative-control` — three
transcripts of a misbehaving broker replayed through the identical assertion block, 8 of 8
credential arms red on all three — and it addresses no cluster, so it is a line in `dev/L0-CHAIN.txt`
rather than an L2-only ceremony.

**Five things the first run found, all of them in the fixture or in the spec, none in the broker.**
The broker was correct on every scenario from the first request it ever served.

1. The probe died at the plaintext scenario. `exc.read()` inside the `HTTPError` handler raised
   `ConnectionResetError`, which escaped and took four scenarios with it. Bodies are now read
   through a helper that cannot raise and reports the unreadable body as itself.
2. `trigger.source: "verification"` is not in the Go closed set of seven, so the baseline envelope
   was a 400. Fixed to `cron`; the 400 arm is now a loud failure rather than something tolerated,
   because it means the envelope never reached the pipeline and 4b-ii's soak would be built on the
   assumption that it did.
3. Plaintext is not a transport error. `net/http`'s TLS listener answers with a bare `400` before any
   handler — so the arm asserts _no handler answered_ (no `reason` field), not _no answer_.
4. The audience arm was red for 200 characters of display truncation. See the V-BRK-008 row.
5. `V-BRK-017`'s stated mechanism does not happen against a real API server. See the V-BRK-017 row.

**Findings filed, not fixed — none of them this unit's.**

- **An RBAC denial inside the pipeline surfaces as a 500 `internal-error` with a stack trace.** The
  baseline envelope is authenticated, decoded and classified, and then step 3 fails:
  `configmaps "..." is forbidden: User "system:serviceaccount:kubeagents-system:platform-<scope>-actor"
cannot get resource "configmaps"`. That is an entirely expected, caller-visible condition in dark
  mode — the actor is bound to no tenant authority by design — and `server.go`'s `refuse`/`write`
  have no typed `*Refusal` for it, so it falls through to the unclassified arm. A caller cannot tell
  a permission boundary from a broker bug. **P9-T8b-4b-ii cannot be built on this** and it is that
  unit's first order of business: either the pre-state snapshot's `Forbidden` becomes a typed
  refusal, or the soak fixture grants the platform actor read on its own namespace, and the
  distinction between those two is a real design question rather than a fixture detail.
- **`invariants-gate.py`'s LSN-005 check reads the FIRST `case "$CTX" in` in a file, including one
  inside a comment.** Found by walking into it: this suite briefly wrote its guard as
  `case "$MODE:$CTX"`, which is equally correct and which the gate cannot parse, and the comment
  explaining the fix then contained the literal idiom and shadowed the real guard below it. The
  false-positive direction cost ten minutes. The **false-negative** direction is the one that
  matters and it is live: a script whose comment shows a well-formed anchored guard and whose actual
  guard is a substring match would pass, which is LSN-005 itself, wearing a comment. For
  `harness-improve`.
- **Three `V-*` rows overlap and none of them says so.** `V-BRK-008` and `V-BRK-017` state the same
  property under two IDs; 09 §6 gives the plaintext arm to **both** `V-BRK-007` ("a plaintext or
  wrong-CA client is refused") and `V-BRK-009` ("valid token over plaintext"). Shared evidence is
  recorded as shared in the results rows rather than double-counted. Retire-never-delete applies, so
  this is §3.4 pruning work, not something to act on mid-unit.
- **`V-BRK-017`'s 09 §6 wording needs to say which level owns which clause** — the mechanism it
  names is unreachable at L2 by construction. Written out in full in its results row.

**One defect this unit introduced and then mechanized.** Extracting the eight credential assertions
into a function left the call site unwritten for exactly one commit. The suite ran, printed six green
lines, printed `PROVEN: V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 at L2`, and exited
0 — having asserted none of them. Nothing could have caught it: `fail` stays 0 when no assertion
runs, and the `¬` mode calls the extracted function directly, so it was green too. The suite now
counts its own arms and fails the run if the count disagrees with `EXPECTED_ASSERTIONS`; that guard
was itself verified by temporarily setting it to 15 and watching the run go red. **A suite that
reports a verdict it did not compute is worse than a suite that fails**, and this is a general shape
— worth taking to `harness-improve` as a candidate rule for every `dev/verify/*.sh`, not just this
one.

### P9-T8b-4b-ii splits: 4b-ii-1 is a typed refusal, 4b-ii-2 is the soak

4b-i's first filed finding said the 500-on-RBAC-denial was 4b-ii's **first order of business, and
that the choice between typing the refusal and granting the actor a read was a real design question
rather than a fixture detail**. Investigating it answered the question and also showed that 4b-ii
was two units, not one.

**The design ruling: type the refusal. Both, in fact, but this one first and on its own.**

`k8s-operator/scripts/agent-identity.yaml.template` is unambiguous — _"Phase 9's actor authority is
the broker-operations grant and nothing else, and binding a cloud-write credential to it now would
hand the actor months of authority ahead of the controls that are supposed to bound it (P10-T1 owns
that)."_ So the shipped actor genuinely cannot read a tenant ConfigMap, and 06 §4.4's fail-closed
table already has the row: **cannot persist a pre-state snapshot → refuse that envelope.** What the
table does not say is _which_ refusal, and there turn out to be two, distinguished by whether
retrying could ever help. That is the whole of V-BRK-031.

Three things made it worse than a cosmetic status code, and the third is the one that decided it:

1. `ReasonSnapshotFailed` already existed and was wired only for the **persist** half at brake row
   4, never for the **capture** half at step 3. Half a reason code, in production, for a phase.
2. `Refusal.RetryAfterSeconds`'s own comment says _"zero means do not retry, which is the right
   answer for every schema and authorization refusal"_ — and this was the site not honouring it. A
   fleet told to wait sixty seconds and try again spends the rest of the phase retrying a permission
   boundary.
3. **`Journal` and `SecurityEvent` are fields ON the `Refusal`.** With no `Refusal`, there is no
   journal entry and no event: the envelope's disposition was recorded **nowhere**. An agent
   enumerating what it may touch left no trace at all, which is the exact opposite of what 06 §4.1's
   per-reason table exists to guarantee, and it is what makes a probing pattern findable afterwards.

The forbidden arm therefore journals and deliberately does **not** alarm. 03 §6's security events
are for identity violations — a caller that is not who it says it is — and `forbidden-caller`
(V-BRK-010) remains that case. This is an authorization outcome for a correctly authenticated
caller, and in shadow mode it fires on **every single action**, so an alarm here is an alarm that
gets muted, at a cost paid by the events that do matter.

**Why the soak could not simply follow in the same unit.** Because the same reading kills the naive
soak too. With the shipped grant, no envelope targeting a tenant resource reaches step 4 — so the
`DryRun` population is **empty**, and this file's own argument against a vacuous V-REV-001 (_"a check
over an empty population is a check that cannot fail (V-MET-014), and shipping one is worse than
shipping none"_) applies to the shadow instance exactly as it does to the executed one. The soak
needs planning defect 2's **read-only** tenant overlay, which does not exist yet. That overlay is
not a security weakening — invariant 7's mechanized allow-list is `get`/`list`/`watch`, and read
verbs are explicitly _not_ authority; it is the **write** half, owned by P9-T9b, that needs all
three guards.

| Unit               | What                                                                                                                                         | Checks                | Blocked on |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- | ---------- |
| **P9-T8b-4b-ii-1** | Step 3's three live reads answer a typed `*Refusal`, split by whether retrying can help                                                      | **V-BRK-031** (L1+L2) | 4b-i       |
| **P9-T8b-4b-ii-2** | The read-only tenant overlay `dev/verify/fixtures/actor-tenant-grant.yaml`, planning defect 2's guard 1, the envelope corpus, journal mining | **V-REV-001** (L2)    | 4b-ii-1    |

4b-ii-1's L2 evidence is free: `broker-auth-l2.sh` already drives an envelope past authentication
into step 3 with the shipped actor, which _is_ the condition. Its 5xx arm was already reporting it.

### P9-T8b-4b-ii-1 — outcome, 2026-07-30

**Green at both levels.** L1: the 3 × 3 table over `{pre-state snapshot, restart baseline,
live-state resolve} × {Forbidden, Unauthorized, transient}` plus the negative control, in
`internal/broker/pipeline/pipeline_test.go`. L2: `broker-auth-l2.sh` at 14 PASS / 0 FAIL, rc 0,
where the **real** API server denied the **real** actor and the broker answered
`403 target-forbidden`, `retryAfterSeconds: 0` — where the run three days ago answered
`500 internal-error`.

**The `¬` is on the discrimination, not on the happy arm**, and that is the point. A check asserting
only "step 3 produces a refusal" passes on an implementation that answers 403 for everything, which
is precisely how this gets written wrong — the RBAC denial is what anyone debugging it sees. So
`TestLiveReadRefusalDiscriminatesRatherThanDefaulting` asserts against `liveReadRefusal` directly
that the two classes differ in reason, differ in status, and that exactly one is retryable.
`verification/mutants/V-BRK-031.json` scores **8/8 caught**, and its rows are chosen so that six of
the eight produce a perfectly well-formed `*Refusal` that says the wrong thing — a check that only
looked for "not a 500" would be green on every one of them.

**A `NotFound` is not in the table on purpose.** `CaptureAll` swallows it as a create's legal empty
pre-state and `CaptureRestartBaselines` baselines it at zero; the rig's default reader is
`absent: true`, so every other test in that file already runs that path. Restating it here would be
a case this helper does not own.

**The L2-0 arm now discriminates on the reason, not the status.** It read
`401 | 403) bad "refused at the AUTH layer"`, which was correct while `forbidden-caller` was the only
403 the actions route could produce. There are now two, and they are opposites: one is the
authenticator saying _this caller is not this broker's agent_, the other is the broker's own actor
identity hitting its ceiling. Leaving the arm alone would have turned a green suite red on a broker
doing exactly the right thing (halt condition 2). Guardrail 9 does not bite — the arm was
**passing** the 500 with a NOTE, so this is not a check edited to let a failing implementation
through — and the amendment is a strict **narrowing**: every reason that failed before still fails,
one previously-impossible reason is now named, and five new `¬` rows in the L0-runnable
`--negative-control` mode pin it, including _"a genuine auth refusal is still a failure"_ so the
narrowing cannot swallow the case the arm originally existed for. Its 5xx arm also stops
passing-with-a-note: the defect it was tolerating is closed, and tolerating it again would mean the
arm can no longer tell the tracked defect from a new one.

**Findings filed, not fixed.**

- **The scratch cluster's actor is `platform-your-gcp-project-id-actor`.** The scope leaf is the
  literal placeholder string from `vars.sh`, and it has been baked into a real ServiceAccount name,
  a real RoleBinding, and every RBAC denial message the broker logs. Harmless on scratch and wrong
  everywhere: the identity a write is attributed to is derived from it, so an install that never
  edited `vars.sh` would attribute every action to `your-gcp-project-id`. Nothing validates that a
  scope leaf is not a template default. A candidate gate rule for `harness-improve`, and it belongs
  with the existing `<scope>`-segment ambiguity already open in the ledger.
- **The probe's 400-character detail cap truncated the denial mid-namespace-name.** This is the same
  shape as 4b-i's finding 4, where a 200-character display cap cut the substring an assertion was
  reading. It is not load-bearing _yet_ — V-BRK-031's assertions read the reason, the status and the
  retry, none of which are in the detail — but the projector already caps at 1000 and the probe caps
  again at 400, so there are two caps and only one of them is documented as dangerous. For
  `harness-improve`, with 4b-i's finding.

### P9-T8b-4b-ii-2 splits: 2a is the overlay and its lint, 2b is the soak

Oversized on inspection, so split per `harness-run` §2. **2a** — the read-only tenant overlay and
planning defect 2's guard 1 — is hermetic L0 and depends on no cluster. **2b** — the envelope
corpus, journal mining and V-REV-001 at L2 — is a new L2 suite with a corpus and a `¬` mode. The
order follows this phase's own recorded rule that all remaining L0 work goes in front of the
remaining L2 work, so the images are built once against a tree that has stopped moving (every commit
invalidates P1).

| Unit                | What                                                                                        | Checks             | Blocked on |
| ------------------- | ------------------------------------------------------------------------------------------- | ------------------ | ---------- |
| **P9-T8b-4b-ii-2a** | The read-only tenant overlay, its applier, and the lint that confines it to `dev/`          | **V-CTN-037** (L0) | 4b-ii-1    |
| **P9-T8b-4b-ii-2b** | The envelope corpus soak over the overlay, journal mining, and guard 3 as a label assertion | **V-REV-001** (L2) | 2a         |

### P9-T8b-4b-ii-2a — outcome, 2026-07-30

**Green.** `dev/verify/fixtures/actor-tenant-grant.yaml` grants the deployed actor `get`/`list`/
`watch` on six workload kinds in one tenant namespace; `dev/lib/actor-overlay.sh` renders it,
applies it, and does not return until the API server's own authorizer agrees — and, in the same
breath, that the actor still cannot write there and still cannot read `kube-system`.
`check_test_only_grants_are_confined` (**V-CTN-037**, new in `invariants-gate.py`, 22/22 green) is
guard 1, with 12 negative controls in `dev/test_invariants_gate.py`.

**The labels were the whole design, and reading `vap-agent-readonly.yaml` settled them.** The
overlay carries `kube-agents/tier: platform` and deliberately **not** `kube-agents/role: actor`:

- `kube-agents/role: actor` is V-BRK-013's discovery key, and that check asserts every object
  wearing it equals 06 §2.2.1's twenty triples **exactly**. A fixture wearing it turns a green
  BLOCKING-ALWAYS check red — correctly — and the one-line green is an exception in the check.
- `kube-agents/tier` is `is-agent-rbac`'s predicate, so the overlay's read-onlyness is enforced at
  admission by the shipped policy rather than merely asserted by the file granting it. It is also
  invariant 7's predicate, which puts the overlay **inside** that invariant's population rather than
  beside it. Read verbs are explicitly not authority there, so nothing is weakened.

**A design question for T9b, surfaced here and not answerable here.** A **write** overlay cannot
wear `kube-agents/tier` — validation 1 denies it — and without the label it is governed by no
admission rule at all: `vap-agent-scope` does not exist until P10-T1, and `vap-agent-readonly`'s
`matchConstraints` cover `roles`/`clusterroles` but not `rolebindings`. T9b has to rule. Until it
does, guard 1 is the only thing between a test fixture and a real over-grant, which is exactly the
weight planning defect 2 assigned it.

**Guard 3 costs nothing because teardown never deletes a namespace.** The applier creates-or-reuses
the tenant namespace and `actor_overlay_revoke` deletes only the Role and the RoleBinding — revoking
the authority, which is the point. No script deletes a namespace, so `cluster-check-hygiene.py`
property 2 ([[LSN-045]]) is never engaged. Guard 3 becomes a label assertion in 2b.

**The lint is derived from a marker, and its scope limit is stated rather than silent.** Discovery
is by `kube-agents/test-only-grant`, never by a path — a rule keyed to one filename is a headcount
of one ([[LSN-036]]). Heredoc RBAC inside a `dev/**.sh` is **out of scope and said so in the
docstring**: a heredoc's disposition is not statically derivable, and `negative-attenuation.sh`
applies a ClusterRole granting `impersonate` on purpose, as an adversarial input proving the VAP
rejects it. Marking that would be a lie and exempting it by helper name would be an enumeration. The
consequence runs the other way too — the rule is enforceable on files and not on heredocs, so the
fixture was made a **file** in order to be inside it.

**`dev/test_review_gate_paths.py` caught the fixture on the first full chain run.** A file that
decides authority and does not trigger the security review gate is exactly what V-MET-007 derives,
and it named the new fixture within seconds of it existing. A `dev/verify/fixtures/**` glob — a
directory, not the filename, because nothing outside `dev/` may name such a file — was added to
`review-gate.yml`. This is the harness catching the harness, and it is worth recording as a
non-defect.

**Then the new check caught its own evidence row.** The first `verification/results.csv` row written
for V-CTN-037 quoted the marker verbatim and named the fixture by basename, and P1 and P3 both fired
on it: a CSV outside `dev/` is not prose, and the check does not know the difference between a
record of a grant and a path that applies one. The temptation was to add `verification/` to the
allow-list beside `.md` — which is a check edit in the unit that authored the check, and a widening
of exactly the kind [[LSN-036]] warns about, since every future evidence row would inherit the hole.
The row was reworded instead: it describes the marker rather than spelling it and points at
`dev/verify/fixtures/` rather than the filename. Cost: one sentence. The standing rule that follows
is worth more than the row — **evidence about a test-only grant describes it, it does not quote it**,
and the check enforces that for free.

**Findings filed, not fixed.**

- **`dev/assertion-baseline.json` is stale by roughly 1 000 assertions.** The committed baseline
  holds **34 files / 194 named tests**; the tree today yields **131 / 1 209**. The ratchet
  (V-MET-003, BLOCKING-ALWAYS) therefore has a floor 84 % below the actual assertion count and would
  not notice a thousand assertions being deleted. It is passing — `inventory() ⊇ baseline` — which
  is why nothing has said so. This unit regenerated it, saw the size of the jump, and **reverted**:
  raising a security ratchet by 1 015 names is a review event that deserves its own commit and its
  own reasoning, not a ride-along in a fixture unit. The ratchet is no weaker than it was this
  morning. For `harness-improve`, and it is the highest-value item on that list.
- **The heredoc half of V-CTN-037.** Closing it needs a way to tell an applied grant from an
  adversarial input. Two scripts already grant that way: `brake-fanout-l2.sh` applies and keeps a
  Role with `create`/`patch`/`update` on ActionRecords, and `negative-attenuation.sh` applies four
  documents of which three are supposed to be denied. For `harness-improve`.
- **`actionlint` is not installed on this host**, so the `review-gate.yml` edit was checked by
  prettier and by `dev/test_review_gate_paths.py` (which parses the workflow and reads
  `on.pull_request.paths`) rather than by the linter CLAUDE.md names. The edit is one entry appended
  to an existing list. A candidate precondition for `binding.md`: a workflow edit with no actionlint
  available is a stated gap, not a silent one.

### P9-T8b-4c — outcome, 2026-07-30

**Green: `dev.test_action_envelope` 44 tests, `dev.test_envelope_wire_keys` 6 tests, both exit 0;
full `dev/L0-CHAIN.txt` clean; `invariants-gate.py` 22/22; `spec-ids.py` OK at 251 IDs. Mutation:
V-BRK-028 20/20 caught (grown from 16), V-BRK-032 6/6 caught.**

The scheduled finding was one word: `session_trace()` put `parentSpanId` on the wire and
`broker.Trace` has no parent. The fix is `spanId`, which **preserves the information rather than
dropping it** — `ActionRecord.SpanID`'s own doc comment reads "the originating span", which is
exactly what the agent runtime's `SPAN_ID` is, and 06 §4.1's "a genuine retry necessarily carries a
fresh nonce and a fresh `spanId`" reads the same way. Discarding the value would have been the
cheaper diff and the wrong one.

**The defect had a parent, and the parent is a hole in a check.** `envelope.go` declares **six**
closed enums. `action_envelope.py` mirrored **three**. And `TestEnumsMatchTheBroker` — the class
whose entire job is "the two sides agree on the closed sets" — was three hand-written tests naming
those same three. **The set under test was the set that agreed.** The class could not have failed on
the three missing mirrors, because it did not know they existed.

That same hole had already fired live and been misread. `trigger.source: "verification"` came back
`400` during P9-T8b-4b-i and was written up as a fixture typo. It was not: nothing agent-side knew
`trigger.source` was closed, so nothing agent-side could refuse it, and because `DecodeEnvelope`
runs `DisallowUnknownFields` the broker's answer is total rather than field-scoped. Two symptoms,
one structure.

**So the response is `harness-improve` §3.2: strengthen the check that should have caught it.**
`TestEnumsMatchTheBroker` no longer names anything. It discovers every `valid<Name> =
map[string]bool{…}` in the Go source, maps each to its Python mirror by name, and asserts the two
**name sets** are equal in both directions before comparing members. Adding a fourth hand-written
test beside the other three would have closed today's gap and left the seventh enum exactly as
invisible as the fourth, fifth and sixth were this morning — and it would have read as progress.

**The vacuity guard is the equality, not a count.** The first draft asserted
`len(found) >= 6`, which is an enumeration of a number and goes stale the moment a seventh enum
lands. Two-directional name-set equality cannot pass vacuously: zero discovered enums is six
unexplained `VALID_*` constants on the Python side, and it also fails on a Python constant naming an
enum the broker does not have. It earned its keep immediately — the first discovery regex found five
of six, because `validRequesterKinds` is a one-line literal whose lazy DOTALL body ran past its own
closing brace and swallowed `validPlatforms` whole. That surfaced as a vacuity trip, **not** as a
member mismatch. A check whose failure mode is "I found fewer things than exist" is a check that
needs an arm looking at the count of things it found.

**The empty string is a member, not a falsy value.** `validPlatforms` and `validPropagation` both
carry `""`. A mirror that filtered on truthiness would reject every envelope omitting an optional
field — so the derivation copies members verbatim and never interprets them.

**V-BRK-032 is a second ID because it is a second property, and the split is declared.** The enum
join stays under V-BRK-028, whose file owns it. V-BRK-032 is the direction nothing covered: _every
key the agent builds is a key the decoder accepts_, and every key the decoder requires is one some
builder emits. It is asserted structurally over the builders' ASTs and then **measured against the
real `broker.DecodeEnvelope`**, compiled once and run on a maximal envelope from each tier. This
phase's findings list already carries three `V-*` rows that overlap without saying so; that is why
the split is written down rather than assumed.

**Two of this unit's own mistakes, both about the harness voting.** An unused `encoding/json` import
made the first decode program fail to compile — `rc 1`, indistinguishable from "the broker refused",
and green for the wrong reason. It was caught only because
`test_the_decoder_is_the_strict_one_this_check_assumes` demands the refusal **name** `parentSpanId`.
The build now happens once in `setUpClass` behind a loud assert, so a harness that does not compile
cannot produce a verdict at all ([[LSN-048]], [[LSN-049]]). And
`test_the_trace_key_the_defect_was_is_not_back` went red on the docstring explaining why
`parentSpanId` is gone — [[LSN-023]] in miniature — now scoped to AST string literals minus
docstrings: prose may discuss it, nothing may build it.

**M19 escaped the first sweep and the escape was real.** The consulted-ness test was a substring
search over `_check_client_side`, and every one of these constants is also interpolated into the
`EnvelopeError` it raises — so the check passed for a validator that inlines the members and
mentions the constant only when explaining the refusal. Rewritten to walk `ast.Compare` operands.
The mutant was rewritten to match: it swaps the comparison's operand for an inline `frozenset`
literal and leaves the error message referencing the constant, so behaviour is identical, every
other test stays green, and only the consulted-ness arm can catch it.

**Then the new enforcement found a third instance, and it was the worst one.** With
`VALID_TRIGGER_SOURCES` enforced client-side, `dev/test_broker_client.py` went red — eleven arms,
all reporting "nothing was POSTed". The cause is one line of shipped code:
`submit_action` passed `trigger or {"source": "agent"}`, and **`agent` is not one of the seven.**
Not a latent defect like `parentSpanId`, which needed `SPAN_ID` set, and not a fixture typo like
`trigger.source: "verification"`. This is the **default**, on the one mutation tool, reachable only
through an MCP server whose `submit_action` has no `trigger` parameter at all — so **every write
every agent could make was a `400 invalid-envelope`**, and had been since the file was written. It
survived because nothing has yet driven the MCP tool against a live broker: T8b-4b-i's driver builds
envelopes directly, and it uses `cron`.

A red sibling suite is halt condition 2, and this one is not a halt: the suite went red because the
implementation is wrong, the diagnosis is complete, and the fix is in the implementation. Nothing
about the check moved.

**The default is now `chat`, and the choice is not arbitrary.** 06 §4.1 splits the autonomy buckets
exactly at the interactive line — `humanRequested ∈ {chat, undo}`,
`selfInitiated ∈ {watch, alert, cron, delegation, escalation}` — and this function's only caller is
the MCP tool, which is reachable only from an interactive session. Defaulting to `watch` would file
human-requested work under autonomy in the metrics 01 §7 counts. Every autonomous origin arrives
through a caller that knows which one it is.

**But a default is still a default, and 06 §9 says the tool _takes_ `trigger`.** Making it a real
parameter touches three tiers' MCP tools and the `apply-change` skill that teaches them, which is
its own unit: **scheduled as P9-T8b-4d**.

**V-BRK-029 gains the arm that would have caught it**, in
`TestTheGoSideIsTheDefinition` — the class whose stated job is "every value Python restates is read
back out of Go and compared". It walks the `build_envelope` call, unwraps the `x or {…}` idiom the
defaults are written in, and asserts every literal landing in a closed-enum field is a member.
Nothing else could have: the enum mirror agrees with Go (V-BRK-028), every wire key is decodable
(V-BRK-032), the transport is correct (the rest of that file) — and a default is none of those
things. It is a **value**, and until this arm the only values under assertion were the ones the
tests themselves supplied. Sweep grown 15 → 18, **18/18 caught**: M16 restores `agent`, M17 does the
same to `requester.kind` so the scan is not a special case for one field, and M18 renames the call
target so the AST walk finds nothing — caught by the `checked` floor, not by any subTest.

**Findings filed, not fixed.** The escape itself — three hand-written tests where the source had six
enums — belongs on the next improvement pass as an escape, alongside the substring-search shape that
its own error messages satisfied. Both are instances of a check reading a name rather than a
structure. A third, sharper one joins them: **three defects of one class in three units**
(`trigger.source: "verification"`, `parentSpanId`, the `agent` default), and the class is _an
agent-side value the broker's closed schema refuses_. What they have in common is not the enum — it
is that the agent side had **no** local enforcement of anything the broker validates, so every such
defect could only be discovered by a live 400, one value at a time. That is now three mirrors and
three enforcements, and the general question for the improvement pass is whether the remaining
`envelope.go` validations (`hex32Re` on `traceId`, the required-field set, the per-op target
exclusivity) deserve the same treatment or whether the line is drawn correctly where it is.

### P9-T8b-4d — outcome, 2026-07-30

**`trigger` is a parameter now, and the argument for that is not tidiness.** 06 §9's tool table says
`submit_action` "takes `intent` + `operations` + `trigger`, fills `trace`/`requester` from the
session", and the tool took two of the three. T8b-4c could only replace a wrong default with a right
one, which fixes the 400 and leaves the actual problem: **`trigger.source` is the field 01 §7
counts.** It is what splits 06 §4.1's two autonomy buckets, so whatever a default says, it says it
for every caller that did not think about the question — and the direction it is wrong in is the
flattering one. An autonomous action filed as `chat` is a false statement about a human, the
quarter's answer to "how much of this did the agents decide on their own?" comes out too low, and
nothing anywhere reads as an error, because a defaulted enum member is a perfectly legal envelope.
The parameter is **required**, in the client and in both MCP tools: the caller states the origin or
there is no call.

**Flat strings, not a dict, and the reason is two constraints meeting.** V-BRK-029 requires each
`@mcp.tool()` body to be exactly one `return broker_client.<name>(…)` statement — logic in that
module is logic no L0 check can execute ([[LSN-007]]) — so a `{source, ref, detail}` dict cannot be
assembled inside the tool. And the schema the model reads is generated from the signature, so three
flat parameters (`trigger_source`, `trigger_ref`, `trigger_detail`) put the closed enum in the place
the model actually looks. `broker_client` assembles the dict, dropping `ref` and `detail` when empty
because both are `omitempty` on `broker.Trigger` and a blank string is a claim that there was
nothing to look at. The old `trigger: dict | None = None` was **removed** rather than kept beside
the new parameters — two ways to say the same thing is [[LSN-041]], and one of them would have gone
untested.

**Four arms added to V-BRK-029, and one of them exists because the unit deleted the surface the last
one watched.** T8b-4c's scan was pinned to `build_envelope`'s keyword defaults, which is where that
defect happened to live; T8b-4d removed the default, so the scan would have walked zero literals and
gone on reporting green. It now walks **every dict literal in the module** — the property was always
"no closed-enum value originates in this file unless it is a member", and `session_requester`'s two
`kind`s are inside it for the same reason the trigger was. Beside it: the origin is read back **off
the wire** for both tools in all three tiers (the first assertion anywhere that `trigger` survives
the trip), `trigger_source` is asserted to be in the no-default set of all four functions, and each
MCP tool's declared parameters are compared against what its one statement forwards — **by name**,
so `trigger_ref=trigger_detail` is caught too. Sweep grown 18 → 22, **22/22 caught**, with M16
rewritten to re-add a default whose value is _correct_, because the shape is the defect.

**V-CTR-020's two new mutants escaped on the first sweep, and this unit's own prose is why.** The arm
that guards required parameters asserted only that the backticked name appeared _somewhere_ in the
skill. The mutants delete a parameter's **definition** — and the paragraphs T8b-4d added to the
worked example mention both `intent` and `trigger_source` in passing, which kept the arm green over
a skill that no longer explains a required parameter. Being mentioned is not being documented; it is
[[LSN-023]] at one remove, a check satisfied by prose about the thing rather than by the thing. The
arm now requires the definitional bullet the file already uses for all three (`- **`name`** — …`),
which a cross-reference does not have. The second new mutant, M14, drops `escalation` out of the
seven-row table: every other arm passes on it, and an agent that was escalated to would pick the
nearest word it can see.

**Findings filed, not fixed.** The forwarding arm covers the MCP tools only; `plan_action` in
`broker_client.py` delegates in exactly the same shape and is covered only behaviourally (M21).
Generalizing "a single-statement delegation forwards every parameter it declares, under its own
name" to the whole write path is an improvement-pass item. And three needles in V-CTR-020 and two in
V-BRK-029 went `BROKEN` when the signatures and the skill text moved — not findings ([[LSN-048]]),
but five in one unit is the first time a spec's needles have been this brittle, and needles anchored
on a signature line are the pattern.

### P9-T7c-2c — outcome, 2026-07-30

**The number was never in the source.** V-BRK-021 asserted "one listening port, **one mutating
route**" and cited 03 §4.1. 03 §4.1 contains no route count. What it contains is _"there is no other
write path"_ and _"steps 1, 3, 4, 5, 6 and 11 are not skippable by any caller"_ — properties of the
**pipeline**, not of an integer. "One mutating route" was a faithful proxy while exactly one route
existed and became wrong the moment 05 §1.3's `replay` opened a second door into the same corridor,
which is what halted T7c-2b on 2026-07-29. A human ruled option (a) — reshape the check — and this
is that reshape. It is recorded as a **strengthening**, and PROTOCOL §10.2 is satisfied by the ruling
rather than argued around: §10.2's remedy for weakening a BLOCKING-ALWAYS check is a halt for human
review, and the review is the thing that scheduled this task ([`BACKLOG.md`](BACKLOG.md) B-003).

**The shape.** `MutatingRoutes()` was `[]string{ActionsPath}` with a doc comment reading "Exactly
one, and asserted." It is now `Registered()` less a declared non-mutating allowlist, where
`Registered()` is written by `handle` — the single function that touches the mux. The subtraction
runs in that direction on purpose: **the small set is the declared one**, so a route someone adds
and forgets to think about lands in the _mutating_ set, where the 05 §1.3 subset assertion refuses
it. Declaring the mutating routes and treating the remainder as harmless makes forgetting invisible,
which is precisely what the hand-written literal did.

**Four properties replaced one number**, and the count they replaced is now a consequence rather
than an assertion: equality against the registered set, subset of the design table, an allowlist
bounded to the three genuinely inert paths, and — new, and the clause that makes "non-skippability"
mean what 03 §4.1 says — every mutating route reaches `Authenticator.Authenticate` and
`Pipeline.Submit`, read off the call graph. A handler that answers 202 without touching the pipeline
is a write with no journal entry and looks like success to its caller; nothing before this looked
for it.

**Two escapes, and the reason is general enough to be worth the paragraph.** The first sweep caught
8 of 10. M3 rewrote `MutatingRoutes()` back into a literal and M4 rewrote `Registered()` into one —
and every set relation in the new test still held, because **on a server with one mutating route a
correct literal and a real derivation return the same answer**. "Derived, not declared" is not a
property of a single observation; it is a property of how the reporter responds when the input
changes, and a test that observes one server can never see it. Closed by building a second server
that registers a path nothing else knows about: a literal cannot mention it. The same blindness
applies to any check of the form "the accessor agrees with the facts" where the facts have only ever
had one shape — and it is the second time in three units that a new check's first sweep found it
weaker than its author believed, which is the argument for the sweep being part of VERIFY rather
than a flourish.

**One hole the sweep found in the design, not just the check.** M2 adds a route _and_ declares it
non-mutating: it never enters the set the equality and subset arms measure, so both hold. The first
draft's M2 was caught only because it happened to use a path `TestNoDebugRoutes` probes by name —
caught by a guess, which is not caught. The real closure is the third arm: `nonMutatingPaths` may
name only `/healthz`, `/v1alpha1/nonce` and the catch-all. A route excusing itself into the
allowlist now fails on the allowlist.

**What this did not do.** It did not implement `/replay` or `/approve` — those stay in Phase 10
beside P10-T4/T7, as one unit against one reshaped check, and the T7c-2b deferral row closes on the
09 edit exactly as its promotion condition said. It did not settle V-BRK-021's **L2** half: the
2026-07-29 P9-T9 recon records it needing L0+L2 with only L1 evidence on file while the deferral row
records it green at L0, and that reconciliation is **T9b's**. Touching a row does not earn the right
to answer a question about it. The re-entry clause is in the row as a conditional over an **empty**
population, and the suite logs it as empty rather than satisfied.

**Retired with it:** `strings.Count(src, "s.mux.HandleFunc(") != 4`. It did catch a smuggled
handler, and it also went red on every legitimate route, so its maintenance instruction was "raise
the number until it passes" — a check you edit to make it pass is a check that will one day be
edited past a real finding. Its replacement asserts that the count of _registration points_ is one,
which no legitimate route addition changes.

### P9-T8b-4b-ii-2b splits again: 2b-i wires the validator, 2b-ii is the soak

**The fifth time surveying the soak turned up a defect in shipped code rather than a gap in the test
scaffolding.** 4a found `KUBEAGENTS_BROKER_IMAGE` set nowhere; 4b-i found no client that could hold
a certificate; 4b-ii-1 found an RBAC denial surfacing as a 500; 2a found a write overlay with no
admission rule to govern it. This one is larger than all four.

**`undo.GenerateAndValidate` has no non-test caller.** The function whose own doc comment reads "the
call the broker actually makes at step 6" is called by nothing outside its own tests.
`pipeline.Config.Planner` is a `Generate`-only seam that defaults to `undo.Generate`;
`cmd/broker/wiring.go` leaves it unset and says so in its header — "the undo planner … is left unset
so the owning package supplies it" — and the owning package supplies the half that does not
validate. There is no `undo.DryRunner` implementation anywhere in the tree outside `undo`'s own
tests. Consequences, each read off the code rather than inferred:

- Every `ActionRecord` the shipped broker has ever written carries `undoPlan.validated: false`.
- `undo.ValidateReplayable` refuses on exactly that field — _"the undo plan was never dry-run against
  the API server, so nothing has checked that its steps would apply"_ — and it is the front door of
  both replay paths (`verify/driver.go` and `rollback.Rollback`). **Undo is non-functional end to
  end**, and the way a human would find that out is by trying to undo an outage.
- 06 §4.3.1 is normative that validation happens and that failing it raises to `gated`. That arm
  cannot fire at all today.
- **V-REV-001 at L2 is therefore 0 %, not 100 %** — which is the exact property 2b was built to
  measure. Running the soak first would have produced a red with no diagnosis attached.

The escape shape is 09 §11.9, _component built, never wired_. V-REV-003's L1 row (2026-07-27, P9-T4)
proved that `Validate` **downgrades correctly when the dry-runner is nil**. It proved the function
and never the wiring, and its own evidence note says so without noticing: "an unwired dry-runner …
each is a downgrade, not an error."

| Unit                  | What                                                                                                                               | Checks             | Blocked on                           |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------ |
| **P9-T8b-4b-ii-2b-i** | Wire the validator: a required `DryRunner` on the pipeline, a real one over the actor's client, step 6 refuses an unvalidated plan | **V-REV-003** (L1) | —                                    |
| **2b-ii**             | The envelope corpus soak, journal mining, guard 3 as a label assertion, **V-REV-001** at L2                                        | **V-REV-001** (L2) | T9b's **write** overlay + its ruling |

2b-i goes first under this phase's own L0-before-L2 rule, and it is unblocked. **2b-ii is blocked and
the blocker is structural, not scheduling.** A server-side dry-run write is authorized as the write
verb, so under the read-only tenant overlay every undo step's dry-run is a 403, every plan downgrades
to `none`, and every action gates — a correct broker reporting 0 % coverage for a reason that has
nothing to do with undo. The last mile is the **write** overlay, which is T9b's and which is itself
waiting on the admission ruling 2a surfaced.

### P9-T8b-4b-ii-2b-i — outcome, 2026-07-30

**Undo is wired. `undoPlan.validated` is now a fact rather than a field.**

What landed, in the order the seam runs:

- **`pipeline.Config.DryRunner`** — a **required** `func(agentIdentity string) undo.DryRunner`.
  Required, so a broker with nothing to validate with refuses to start rather than serving every
  request and journalling `validated: false`. A **factory** keyed by identity, not a fixed object,
  because server-side apply reports a conflict for every field owned by a different manager and an
  undo commonly restores fields this agent set earlier — a dry run under any other name manufactures
  conflicts the real replay never hits, downgrades working plans, and gates the fleet for a reason
  that is an artifact of the check.
- **`Planner` gained a fourth parameter** and the default moved from `undo.Generate` to
  `undo.GenerateAndValidate`. A signature that cannot express "generate without validating" is what
  stops that returning.
- **`rollback.PlanDryRunner`** — the production `undo.DryRunner`, built **on the `Replayer`** rather
  than beside it. The question plan-time validation asks is not "is this plan well-formed" but
  "would the calls the replayer is going to make succeed", and only the replayer knows which calls
  those are. A validator with its own op table would answer a question about a different program.
- **`ClientApplier.Create` / `rollback.Writer.Create` gained `dryRun bool`**, so the validator goes
  through the one client this broker has instead of opening a second one (LSN-040).
- **`cmd/broker/wiring.go`** hoists the replayer beside the applier and hands the same object to the
  verifier's rollbacker and to the validator's factory.

**The design question 06 §4.3.1 does not answer, and the ruling taken.** An undo plan describes the
world **after** the action and is validated **before** it, so two of the four steps address an object
whose existence is exactly what the action is about to change: the `delete` that reverses a create
gets a NotFound, the `create` that reverses a delete gets an AlreadyExists. Read literally, validation
would downgrade both and gate every create and every delete in the fleet. The ruling: **the dry run
asks "would the API server accept this step from this identity", and those two answers are positive
evidence.** Kubernetes authorizes before it looks the object up — a caller without the verb gets 403,
not 404 — and a create clears mutating and validating admission before storage. Everything else (403,
Invalid, a webhook rejection, a missing scale target, a body `hydrate` refuses) is a failure that
downgrades to `none`, which the 06 §4.2 step 6 floor raises to gated. The one honest gap, DELETE-time
admission running after the fetch, is named in `dryrun.go` rather than papered over.

**A side effect worth having.** Reusing `Replayer.hydrate` moves the redacted-Secret refusal — the
worst thing in this package's blast radius — from replay time, during an incident, to generation
time, where it is a downgrade and the action gates before mutating anything.

**Three sites ask "is there a usable undo plan", not two.** classify's 06 §4.2 step 6 floor, the
pipeline's step 6 re-check, and the brake's 06 §4.4 row 5. Only the first suppresses for a dry run.
The first two now read one predicate, `classify.UndoPlanGateApplies`, so they cannot drift; be precise
about what that buys, because the tempting claim is bigger than the truth — mutating step 6 back to
its own spelling does **not** fail anything, since the brake has already raised the class by the time
step 6 looks. It is a structural fix, and it is asserted directly rather than through behaviour.
**The brake is the outstanding one and is filed, not fixed**: it raises a dry run whose plan cannot be
validated to gated, so it parks for approval instead of previewing. Over-gating, safe, and a row in
the 06 §4.4 table — V-BRK surface, and changing a brake row is a unit of its own rather than something
folded into the unit whose wiring surfaced it.

**A second escape found while verifying this one, and this one is not small.**
`internal/broker/rollback`'s `TestMain` did `os.Exit(0)` when `KUBEBUILDER_ASSETS` was unset. That is
a package-wide skip wearing the word `ok`: **the entire hermetic half of the package — including the
refusal that stops a redacted Secret being written back as sixty-four characters of hex — had never
run under `go test ./...`, which is what the L0 chain and PR CI execute.** The package reported `ok`
in 1.3 seconds while asserting nothing, and it was found only because `dev/mutate.py` refused the
sweep: `go test -list` returned no names, so the catchers "did not exist". LSN-048's guard caught a
defect it was not written for. Fixed to the shape escalate, history and writeahead already use — the
environment is optional, the six envtest tests skip individually via `requireEnv`. `probe` still has
the old shape; it has no hermetic tests today, so it costs nothing yet, and it is filed.

**Findings filed, not fixed** — three, and the first two are named above. (a) The brake's 06 §4.4 row 5
is the third spelling of the undo-plan gate and does not suppress for dry runs. (b) `internal/broker/probe`'s
`TestMain` still carries the `os.Exit(0)` shape. (c) New, and adjacent to (a): `BrakeInputs.UndoPlan` is fed
`signal(s.plan.Undoable())` at [pipeline.go:708](../../k8s-operator/internal/broker/pipeline/pipeline.go#L708)
and [:852](../../k8s-operator/internal/broker/pipeline/pipeline.go#L852) — the **weaker** predicate, the one
step 6 stopped asking in this unit. It cannot under-gate today, because `Undoable()` is implied by
`Validated()` and the brake only raises, so a plan that is undoable-but-unvalidated already reaches the brake
as `true` and step 6 catches it after. But it means the brake's view of "is there a usable undo plan" and the
pipeline's are two different questions wearing one field name, and the next person to add a lowering rule to
the 06 §4.4 table inherits that. Same V-BRK surface as (a) and the same unit.

Evidence: **V-REV-003 (L1) pass** — 13 new assertions across
`internal/broker/pipeline`, `internal/broker/rollback` and `cmd/broker`; mutation sweep
`verification/mutants/V-REV-003.json` **12/12 caught**, including two vacuity controls, with the
mutants aimed at the **wiring** (the required-field arm, the default planner, the composition root,
the identity threading) rather than at the function the old L1 row already proved.

---

## Deferrals opened by this phase (each with a named external blocker)

Recorded at PLAN time so they are visible from the start rather than discovered at the gate. **No
BLOCKING-ALWAYS check appears here** — that is planning defect 2's entire reason for existing.

| Check / bullet                     | Blocker (external, named)                                                                                                     | Owner | Promote when                                          |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ----- | ----------------------------------------------------- |
| V-CMP-006 at **L3**                | The live install `platform-agent-host` must be rebuilt on the fixed render before the inter-agent call can be exercised there | human | The live install is rolled to a build carrying P9-T10 |
| V-BRK-006 at **L4** (soak)         | L4 is the multi-day soak level; no soak harness exists before Phase 14                                                        | —     | Phase 14 stands up the soak lane                      |
| V-REV-008 at **L4** (retention)    | The 30/90/365-day TTL clocks cannot be observed inside a phase; L2 asserts the fields and the deletion predicate              | —     | Phase 14                                              |
| V-RUN-014 at **L3**                | One Socket Mode connection fleet-wide is only observable on the live install                                                  | human | Live install rebuilt                                  |
| Accept (a)/(b) at **L3** (carried) | Standing from Phase 8 — no empty GCP project for a clean-clone install                                                        | human | An empty scratch project exists                       |

The three L4 rows are level deferrals of checks whose L1/L2 instances **do** run in this phase; the
BLOCKING-ALWAYS rule is about a check having no evidence at all, not about its deepest level.

---

## Notes carried into IMPLEMENT

- **P1 before every L2 judgement.** `dev/cluster/reload-images.sh` now has to grow a **broker**
  target (P9-T2). Until it does, no L2 broker claim is admissible — a broker image deployed by tag
  is LSN-001 with a new binary. Build all seven-plus-one images concurrently on Cloud Build; never a
  host-arch `docker build` (the Makefile exits 2 on arm64, deliberately).
- **The tree freezes for the duration of a gate run.** Every new commit invalidates the deployed
  image and therefore P1 for every suite still to come. Rebuild once per unit, not once per commit.
- **P6 / LSN-003:** assert against the **operator-rendered ConfigMap**, not the image-baked
  `config.yaml` it shadows, and name which one the check reads. P9-T10 touches both sites and is
  exactly where this bites.
- **LSN-015:** any per-agent resource is exercised with **≥2 agents in one namespace**. The broker is
  per-`Agent`-CR, so P9-T7's pair rendering must be checked with two CRs, not one.
- **LSN-024 / planning defect 2:** the fixture broker is rendered by the shipped renderer. A fixture
  that diverges from the render is scenery, and scenery passes.
- **The destructive-test guard stays anchored** on `gke-scratch-*`. Every new L2 script in P9-T9
  carries it, and `invariants-gate.py` asserts the anchoring. `platform-agent-host` is never a
  destructive-test target — and in this phase, where the overlay grants real write authority, that
  matters more than it ever has.
- **`internal/router/classify.go` is not the risk classifier.** P9-T3 creates
  `internal/broker/classify/`. Two things named `classify` in one tree is the kind of collision that
  produces a correct-looking import of the wrong package.
- **The classifier reads live state, never prose.** `intent` and `rationale` are journaled and are
  never classification inputs (V-GAT-017 asserts the package imports no inference client and that
  100 permuted rationales yield byte-identical classifications).
- **`ChangePolicy` cannot loosen, and that is structural, not validated-then-trusted**: there is no
  `allow`, no `maxClass`, no `exempt`, no downgrade path in the schema, _and_ the broker takes the
  maximum over all sources regardless. Both halves get a test; either alone is a convention.

### P9-T9b-5a — outcome, 2026-07-30

The unit was scoped as "the tenant overlay's write half, and the admission ruling it needs". Both
landed. The ruling is the interesting half, and it is the first thing this phase has decided that no
check ID covers.

**The question, restated.** `dev/verify/fixtures/actor-tenant-grant.yaml` — the READ overlay, written
in P9-T7d-3 — ends its header by handing T9b something it could not answer at the time: a write
grant for the actor identity is denied by `vap-agent-readonly` validation 1 if it wears
`kube-agents/tier`, denied by validation 3 if it wears `kube-agents/role: actor` (tenant resources
are absent from the twenty-triple 06 §2.2.1 allow-list), and governed by nothing at all if it wears
neither. Three doors, two locked and one that is not a door.

**The ruling: step outside the population, do not bend the policy.** The tempting move is a
carve-out — an exempt label, a namespace exclusion, a `!has(object.metadata.labels['test-only'])`
clause in the match condition. Every form of it is a hole in the one runtime backstop that rejects
bad agent RBAC _after_ human review has already passed it, and the hole is shaped exactly like the
thing an attacker wants: a label that turns the policy off. It is also, mechanically, a weakening of
a BLOCKING-ALWAYS surface, which PROTOCOL §10.2 makes a halt rather than a judgement call — so the
question was never actually open. The fixture wears neither label and is outside `is-agent-rbac` on
purpose.

**The cost is conceded in writing, in the fixture's own header.** The read overlay is bounded by
V-CTN-037, by the two library functions that apply and revoke it, _and_ by the cluster's admission
policy. The write overlay is bounded by the first three and not the fourth. That is a genuinely
weaker position and the header says so in those words rather than presenting the ruling as free.
P10-T1's `vap-agent-scope` is named as what closes it: once an actor's tenant template is a compiled
allow-list, this fixture can wear `kube-agents/role: actor`, be selected by validation 3, and be
bounded by the cluster again. The header is the thing to re-read on the day that lands.

**What makes it a fact rather than a reading.** Until this unit every clause above was a prediction
about what an API server would do, made by reading CEL in a file. `actor-overlay-admission-l2.sh`
submits all three label variants to a real one and records the answers. The three variants are
**derived from the shipped fixture**, not re-typed: the Role document is rendered exactly as
`actor_overlay_apply_write` renders it and each variant is that document with one label line spliced
in. A hand-written stand-in would make all three arms true of a rule set nobody grants, and would
keep passing on the day the fixture grew a fourth resource.

**P2 finally has a function, and the reason it did not is the interesting part.** P2
(policies-are-live) was the only entry in `binding.md` §Preconditions with no `p*_` helper and no
lesson behind it. That was not an oversight: every admission claim this repository had ever made was
a claim that something was **DENIED**, and a denial is self-witnessing — an absent policy, an
unactivated binding and a deleted policy all fail to produce one. This suite's core arm asserts an
object **IS ADMITTED**, which reads exactly the same green against all three of those. That is the
shape of [[LSN-006]] aimed at admission instead of at the dataplane. `p2_assert_policy_live` makes
liveness an experiment — a server-side dry-run of a manifest the policy must reject, polled to the
timeout — rather than a `kubectl get` of stored YAML, and it probes **validation 2** so that
establishing the precondition does not quietly pre-establish either of the arms that follow.

**The first run was red, for a reason nothing in the tree could have found.** L2-1 (P6) reported the
deployed `kube-agents-agent-readonly` carrying **2** validations against the tree's **3** — the actor
carve-out landed in P9-T7d-3 and the scratch cluster's policy was four days stale. L2-3 then failed
as a direct consequence: validation 3 was not there to deny anything. That pairing is the best
evidence the suite is non-vacuous that this unit could have produced, because it was not
constructed — the arm that measures the artifact and the arm that depends on it went red together,
in the right causal order. After `kubectl apply -f examples/gitops-repo/policy/vap-agent-readonly.yaml`
the same run is 7/7 green. **The P6 arm defers with the remediation named rather than re-applying the
policy itself**, deliberately: a suite that silently repairs its own environment is a suite that can
never tell anyone the environment had drifted.

**The library half.** `actor_overlay_can`, `actor_overlay_apply_write` and `actor_overlay_revoke_write`
in `dev/lib/actor-overlay.sh`. The write half asserts considerably more than the read half, and the
asymmetry is the ruling's: two of the three things bounding this grant _are these functions_, so
they ask the authorizer rather than trusting `kubectl apply`'s exit code. `apply_write` checks four
positives and four negatives (no `secrets`, no other namespace, no RBAC group, no cluster scope) and
**revokes rather than returning** on any mismatch, because handing a wider-than-advertised authority
to the suite that called you is worse than failing. `revoke_write` proves the authority is _gone_ by
asking again — the read half does not, and does not need to: a leaked read on a scratch cluster is a
nuisance, a leaked write is what V-CTN-037 exists to prevent. `actor_overlay_can`'s `*/*)` guard is
[[LSN-044]] property 1b and is load-bearing rather than decorative here: this helper takes its
resource as a variable, which is precisely the refactor that makes the static half of that rule
unenforceable, and half its questions are negative — `auth can-i update deployments/scale` asks
about a Deployment _named_ `scale` and answers `no` for a reason that has nothing to do with the
policy under test.

**The suite claims no check ID, and that is not an omission.** There is no row in 09 §6 for "the
ruling this phase made is still true", and minting one would put a line in
`verification/results.csv` for a property no spec states. Same shape as `verify-phase9.sh`, same
argument. It is listed in `dev/L2-CHAIN.txt` all the same — what it protects is a decision, and a
decision nobody re-executes is a decision that was right on the day it was written.

**Carried out, not fixed:** the scratch cluster's `kube-agents-agent-readonly` was four days stale
and **nothing in the tree detected it** — this suite found it only because it happened to need that
exact policy. A general "the deployed policies are the tree's policies" line is a candidate for the
next improvement pass. Separately, `kubeagents-router` is in `CrashLoopBackOff` on the scratch
cluster with 466 restarts over 39 hours, unrelated to this unit and unexamined.

**The denominator moves by one.** T9b-5a is closed; 5b and 5c remain.

### P9-T9b-5b-i — outcome, 2026-07-30

**Landed:** `dev/verify/broker-execute-l2.sh` (new, `--negative-control` 13/13),
`dev/verify/fixtures/broker_execute_probe.py` (new), the driver parameterization in
`dev/lib/broker-driver.sh`, one `dev/L2-CHAIN.txt` line, `L2_CHAIN_FLOOR` 17→18 and
`L2_SCOPE_FLOOR` 26→27 in the same commit.

**Acceptance bullet (a) has now been executed.** "An envelope flows end-to-end in shadow mode and
produces a well-formed `ActionRecord` with a valid undo plan" was the phase's largest unmeasured
claim: four suites proved the undo machinery and every one of them started from an `ActionRecord` a
test had written. This is the first line in the chain whose evidence is an object the **broker**
produced.

#### The claim set was cut in half at IMPLEMENT, and that is the finding

The SELECT-time row claimed V-BRK-006, V-BRK-018, V-BRK-019, V-REV-002 and V-REV-003. Four of the
five are not observable from a single successful shadow submission, and writing the suite is what
surfaced it. Each is recorded here with where it went, because a check ID silently dropped between
planning and implementation is indistinguishable from one that was never planned:

| ID            | Why a shadow submit cannot witness it                                                                                                                                                                              | Where it went                                        |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| **V-BRK-018** | "Snapshot-persist failure ⇒ refuse; neither target applied" is fault injection over a multi-target envelope. Nothing in this suite fails, deliberately.                                                            | **T9b-5b-ii**                                        |
| **V-BRK-019** | The field-manager string is unreadable from a shadow: a server-side dry-run persists no `managedFields`, so there is nothing on the object to read the manager off. Needs a real apply, which Phase 9 does not do. | **Carried, unscheduled** — named in the suite header |
| **V-REV-002** | `undo <id>` restores prior state. Requires executing an undo.                                                                                                                                                      | Later phase                                          |
| **V-REV-003** | "No generatable undo plan ⇒ reclassified gated" is the **negative** of what this suite asserts, and needs an operation whose inverse does not exist. This envelope's inverse does.                                 | **T9b-5b-ii**                                        |

What replaced them is **V-REV-001**, which nobody had scheduled here and which fits precisely: the
record carries a validated undo plan the broker generated. Its row says "100%" and this is n=1, so
the `results.csv` note says n=1 and names P9-T8b-4b-ii-2b-ii's corpus soak as what makes it a
population claim. A suite that reported "100%" off one record would be the exact defect
`broker-auth-l2.sh`'s assertion counter exists to prevent, one level up.

**V-BRK-006 is claimed for its L2 clause only.** 09 §6 lists the row at L2 **and** L4 for a reason:
"the record exists before the mutation" is observable from a running cluster, and "a broker killed
mid-action leaves no unjournaled write" is not. The L2 half is asserted across **two clocks and two
writers** — `metadata.creationTimestamp`, which the API server assigns, against
`status.timestamps.executionStarted`, which the broker stamps when it issues the first mutating
call. Equal stamps pass: RFC3339 here is second-granular and a fast pipeline routinely produces
identical values, so treating equality as a violation would fail the arm for being quick.

#### Two things the suite refuses to read from a second copy

**The legal phase set comes off the served CRD**, not from `actionrecord_phases.go` and not from a
list in the file. Either of those would be the suite agreeing with a copy of the same enum and would
stay green on a cluster serving a different one. The `--negative-control` arm does hold the tree's
list, and that is correct there: it is testing the assertion block, not a cluster, and
`invented-phase` needs a set to be invented against.

**The target object's name comes off the probe's own `note` line.** The suite has to go looking for
an object after the run to answer "did the shadow mutate anything", and the probe is where that
object's identity is decided. A suite that hardcoded the name would check the right object today and
the wrong one the day the probe's constant changed — and checking the wrong object for absence
always passes.

#### The `¬` arm, and the bug it would have had

Thirteen cases: eleven documents each broken in exactly one way, plus a correct one that must go
**green** (an assertion block that failed everything would "catch" all eleven for the wrong reason),
plus two that are not documents at all — the world in which the shadow created the object, and the
world in which the suite could not tell. Every row carries a **needle**, and counts as caught only
when a `FAIL:` line contains it (LSN-035): without that, breaking `jrec` would score 13/13 while
proving the suite is broken.

The first draft read the verdict off `$fail`. `assert_record` runs inside a command substitution,
which is a subshell, so every `fail=1` it set died with it — the arm would have reported all eleven
mutants as escapes. `broker-auth-l2.sh` counts `^PASS:`/`^FAIL:` lines for exactly this reason and
the note is now in this file's header too.

#### P1 fired on the first live run, and that is the second finding

The first invocation against `gke-scratch-kube-agents-dev` stopped at P1: the deployed controller
was `dev-7c4e163` against a tree at `8ffb43e`. This is the ordering rule Phase 9 wrote down for
itself — every commit invalidates P1 for every L2 suite still to come — arriving on schedule, and
the reason all remaining L0/L1 work goes in front of the remaining L2 work. Resolved with
`dev/cluster/reload-images.sh operator gke-scratch-kube-agents-dev`, which deploys by digest.

It then fired a **second** time, on a different image, and that is worth recording separately
because the first resolution looked complete. The broker is not the controller: it is `kage-broker`,
built and deployed by its own `reload-images.sh broker` target, which sets `KUBEAGENTS_BROKER_IMAGE`
**on the controller** rather than on a Deployment, because the operator renders one broker per Agent
CR and `brokerImage()` reads its own environment. P1 for any broker suite therefore requires **both**
targets to have been run, and a suite that checked only the controller would report a current build
while testing a stale broker. Both arms are in the suite for that reason.

#### The defect the suite was written to find, which it found on its first run past P1

**No deployed broker could write any ActionRecord's status. Ever, in any namespace.**

`kube-agents-agent-scope-journal` matches `UPDATE` on `actionrecords/status` and decides whether the
writer is the owning broker with

```cel
request.userInfo.username == 'system:serviceaccount:' + object.metadata.namespace + ':' + object.spec.actorServiceAccount
```

The left side is the ServiceAccount the kubelet projected a token for. The right side is whatever the
broker process stamped into the record, which came from `brokerServiceAccount()` in
`k8s-operator/cmd/broker/main.go`, which read `KAGE_BROKER_SERVICE_ACCOUNT` — **a variable nothing in
the repository set** — and fell back to the literal `"kage-broker"`. That string is the name of the
broker _image_. It is not a ServiceAccount anywhere in this tree; `grep` finds the literal at exactly
one site, and every other occurrence of the token is an image reference. Meanwhile the pod runs as
`actorServiceAccountName(agent)` — `<tier>-<leaf>-actor`, set eleven lines above the container spec.

So the equality compared `…:kage-broker` against `…:platform-your-gcp-project-id-actor` and could not
hold for any agent, on any cluster, at any time.

What makes this the right defect for Accept (a) to have caught is **where** it surfaces. It is not a
startup failure. The broker starts, serves TLS, authenticates the caller, validates the envelope,
classifies it, and _creates_ the record — `Store.Create` succeeds, because the policy does not match
CREATE. The refusal lands on the very next line, `s.client.Status().Update`, which is where
`journal/store.go` writes the initial phase that the CREATE dropped along with the rest of the status
subresource. Every record would sit at an empty `status.phase` with a status _label_ claiming
otherwise — the exact inversion that store.go's own comment says was fixed once already — and every
refusal would go unjournaled, contrary to 06 §4.1.

Nothing could have seen it below L2. The envtest suites use a client whose identity is the test's,
the golden manifest tests asserted the pod's `serviceAccountName` and had no reason to ask what the
process would later _say_ that name was, and the two halves of the equality live in different
repositories of thought: one in `internal/controller/broker_manifests.go`, one in `cmd/broker`. It
required an authenticated agent submitting a real envelope at a deployed broker with the live
admission policies loaded, which is the definition of this suite and the reason Phase 9 exists.

**The fix is the downward API, not a second derivation.** `buildBrokerDeployment` now sets

```yaml
env:
  - name: KAGE_BROKER_SERVICE_ACCOUNT
    valueFrom:
      fieldRef:
        fieldPath: spec.serviceAccountName
```

and `brokerServiceAccount()` has no fallback at all. Writing `actorServiceAccountName(agent)` into
the env would have been the smaller diff and would work today; it would also put the two halves of an
equality the API server evaluates into two places that agree only as long as nobody edits one. Read
off the pod, they are the same value by construction. Removing the fallback matters independently:
`pipelineConfig` already refuses an empty actor service account before the listener opens, and the
default defeated that guard by converting _unconfigured_ into _confidently wrong_ — the strictly
worse state, because unconfigured fails at startup where someone is watching.

`TestBrokerRecordsTheIdentityItHolds` is the L1 guard. It asserts the fieldRef, not the presence of
the variable, and a three-mutant check confirms it: dropping the env block, pointing the fieldRef at
`metadata.name`, and — the one that matters — replacing the fieldRef with a **correct literal** are
all caught. The third is the regression wearing a disguise, and a test that only asked "is the value
right?" would wave it through.

#### The other finding, which was a gap in this suite's own fixture

The run before that one stopped one step earlier, at step 3, with
`403 target-forbidden … cannot get resource "namespaces"`. `classify/resolve.go` reads namespace
**labels** for every operation that names a namespace, because a namespace label is a classification
input — an apply into a namespace labelled production is not the same action as the same apply into a
scratch one. That read happens before any risk class exists, on every envelope.

`actor-tenant-grant.yaml` grants "the kinds the broker's step-3 live reads actually touch" and states
in its own comment that a kind the pipeline never reads is a kind it must not grant. The namespace
object _is_ a kind the pipeline reads, and it was missing — the fixture was incomplete against its
own stated rule. It now grants `get` on `namespaces`, and `get` alone: `GetNamespaceLabels` is a
single-object read, and `list`/`watch` on a cluster-scoped kind are not requests a namespaced Role
can authorize, so granting them would read as authority while conferring none. The RoleBinding lives
in the tenant namespace, so what this actually confers is `get` on that one namespace's own object.

**Filed, not fixed:** the shipped actor grant
(`k8s-operator/scripts/broker-operations-grant.yaml.template`, 06 §2.2.1) has no `namespaces` rule
either, and a production broker needs this read for every namespaced operation it classifies. Those
twenty triples are single-sourced and V-BRK-013 asserts exact equality against them, so adding one is
a ruling with its own unit — the same shape as P9-T9b-5a — and not something to slip into a suite.

#### The third finding was recorded as a halt, and the halt was wrong

With the namespace read granted, the next run reached the same step and stopped one call later:

```
403 target-forbidden — step 3: … resolving lower-tier owner: listing Agents to find a lower-tier
owner: agents.kubeagents.x-k8s.io is forbidden: … cannot list resource "agents" … at the cluster scope
```

That is not another missing line in a fixture. Reading outward from it, **06 §2.2.1's actor grant is
not sufficient for the gates 06 §3 and 06 §4.2 require the actor to run**. Four reads, in the order
the executor makes them:

| Read                                       | Site               | On refusal                                                                                                                                                                 | Covered by the twenty triples?                                                        |
| ------------------------------------------ | ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| namespace labels                           | `resolve.go:88`    | hard error → refuse                                                                                                                                                        | **no** (fixture now grants it; shipped grant does not)                                |
| `list agents`, **cluster-scoped**          | `livestate.go:335` | hard error → refuse, deliberately: "returning [no owner] when the truth is 'I could not look' would drop the gate exactly when the API server is unhealthy"                | **no** — granted namespaced, and a RoleBinding cannot authorize a cluster-scoped list |
| every discovered namespaced kind, in scope | `livestate.go:240` | **tolerated** per kind — the count survives holes — but `listed == 0` is a hard error                                                                                      | no, and mostly should not be                                                          |
| `list secrets`, **in the caller's scope**  | `livestate.go:295` | hard error → refuse; "an empty digest set would report 'no secret material in this payload' for every payload, which is the exfiltration gate answering yes to everything" | **no**                                                                                |

I read that table as a spec contradiction and recorded PROTOCOL §8.5. **That was wrong, and the
error is worth more than the finding.** Every row of the rightmost column asks "covered by the twenty
triples?", and the twenty triples are **06 §2.2.1** — the _broker-operations_ grant. They are not the
actor's authority. **06 §2.2**, one level up and a different object entirely, is the actor **scope**
template, and its header says what it is: "These are the literal rule bodies the render overlay emits
and `vap-agent-scope` validates against. A rule not present here is not grantable to an actor
identity." Read it and every row of the table is answered:

| Read                                       | 06 §2.2 platform ACTOR ClusterRole                                                      |
| ------------------------------------------ | --------------------------------------------------------------------------------------- |
| namespace labels                           | `[""] / [namespaces, serviceaccounts, configmaps, secrets] / [get, list, watch, …]`     |
| `list agents`, cluster-scoped              | `[kubeagents.x-k8s.io] / [agents] / [get, list, watch, create, update, patch, delete]`  |
| every discovered namespaced kind, in scope | partially; and `CountWorkloadObjects` tolerates a per-kind `Forbidden` by construction  |
| `list secrets`, in the caller's scope      | `secrets`, cluster-wide, in the same rule as `namespaces` — deliberately, for this tier |

So the "wall" is not a wall. `SecretDigests` already narrows to the namespace whenever the scope has
one (`if s.Namespace != "" { InNamespace }`); the cluster-wide read arises only for a project-scoped
platform caller, which is exactly the tier 06 §2.2 hands cluster-wide `secrets` to. There is nothing
to rule on, no property to weaken, and resolutions 1–3 were three answers to a question the spec had
already answered. **The halt is withdrawn.**

What is actually true is narrower, and is not a contradiction: **06 §2.2's actor scope template has
no renderer in this tree.** `k8s-operator/scripts/` holds ten templates and none of them is it; no
manifest carries the `kube-agents/scope` label or a `cnrm.cloud.google.com` rule; and its validator
`vap-agent-scope` matches no `name:` anywhere, because it is P10-T1. Unimplemented spec is ordinary
scheduled work, and confusing it for a contradiction cost this unit a halt.

#### The third finding, stated correctly, and it is the same shape as the first

Set the grant question aside and one thing in the table survives on its own terms:

> **The broker's ownership gate issues a cluster-scoped `list agents` on every namespaced operation,
> and nothing the install path applies authorizes it.**

§2.2.1 does grant `agents`, but in the **namespaced** half, and its own comment says what for: "step
5: its own pause state." That is the caller reading its own Agent CR. It is not the ownership lookup.
`LowerTierOwner` lists cluster-wide because it must — it answers "which agent's _scope_ covers
namespace X", which cannot be found by listing in X — and `resolveOwner` has no short-circuit at all:

```go
func resolveOwner(ctx context.Context, live LiveState, caller Caller, op *ResolvedOp) (string, error) {
	if live == nil {
		return "", nil
	}
	return live.LowerTierOwner(ctx, caller, op.Kind, op.Namespace, op.Name)
}
```

`live != nil` is the only guard, so on a freshly provisioned cluster every namespaced envelope fails
closed at the ownership gate. This is the `brokerServiceAccount()` defect a second time and the
resemblance is not a coincidence: a read the broker makes on every call, authorized by no grant that
is actually applied, invisible to every level below L2, and found by the first suite to drive a real
envelope through the pipeline. The thing that would authorize it is 06 §2.2's tier template — the one
with no renderer. So the fix is not a spec edit and not a grant widening; it is the missing renderer,
and it is now scheduled as **P9-T9b-5b-0** below.

#### What this unit landed, and what it did not

Landed and independently verified: the identity defect and its L1 guard, the fixture's missing
namespace read, and the suite, probe, chain line and floors that produced all three findings. The
suite's own verdict on the run is `rc 3 DEFERRED` with the blocker printed — it declined to score
V-BRK-006 or V-REV-001, which is the behaviour it was built to have.

Not landed: V-BRK-006 L2 and V-REV-001 remain unproven. They are not deferrable — both are
BLOCKING-ALWAYS-family and green at no level, and `check_deferrals_name_blockers` refuses exactly
that row. They are blocked on P9-T9b-5b-0, which is implementation work this harness can do, not on
a human.

---

### P9-T9b-5b-0 — the actor grant does not have the shape 06 §2.2 gives it

Scheduled out of the withdrawn halt above; it blocks 5b-i's two unproven claims, 5b-ii and 5c. Its
first draft said "render 06 §2.2's actor scope template, which has no renderer." Reading §2.2 to the
end made the finding both smaller and worse, and changed what has to be built.

**06 §2.2 has a fourth fenced block, and it is a placement rule.** After the three tier templates:

```yaml
# Broker operations — appended verbatim to every actor Role/ClusterRole.
```

and §2.2.1's own prose agrees — "the three actor templates above cover what an agent **acts on**.
They do not cover what the broker needs to **run its own pipeline** … Every actor identity
**additionally** receives exactly this rule set, byte-identical across tiers." So a conformant actor
identity holds **one** object per tier — a namespaced `Role` for developer-team, a `ClusterRole` for
cluster-admin and platform — whose rules are the tier template's **plus** the broker-operations rules
appended.

**The install path builds something else.** `broker-operations-grant.yaml.template` renders a
tier-neutral pair and splits the rules between them on what its own header calls "THE API'S OWN
SEAM":

| Object                       | Resources                                                                |
| ---------------------------- | ------------------------------------------------------------------------ |
| `ClusterRole` (tier-neutral) | `tokenreviews`, `fleetfreezes`, `changepolicies`                         |
| `Role` (per namespace)       | `actionrecords`, `actionrecords/status`, **`agents`**, `approvalrosters` |

That split keeps the rule set byte-identical, which is what §2.2.1's sentence asks for in isolation,
and it silently changes the **scope** of every rule in the namespaced half. §2.2 says these rules are
appended to the tier's own object; for platform and cluster-admin that object is a **ClusterRole**,
so `agents: [get, list, watch]` is a cluster-scoped list. Attached instead to a namespaced Role, the
identical rule authorizes a namespaced list and nothing else.

**That is the whole of the step-3 failure.** `LowerTierOwner` needs a cluster-scoped `list agents`;
06 §2.2 grants it to the top two tiers; the install path renders it namespaced; no identity on any
cluster has ever held it. It is not a missing template and not a spec gap — the authority is
specified, and an object-shape decision made in the render overlay demotes it. The same demotion
applies to `actionrecords` and `approvalrosters`, which is worth checking in the same unit rather
than discovering one at a time.

**Why this cannot be one unit, and the ordering that follows.** `dev/tests/actor-grant-single-sourced.py`
(V-BRK-013) asserts that every object labelled `kube-agents/role: actor` has rules **exactly** equal
to §2.2.1's twenty triples, and its template says a superset fails as readily as a subset. Under
§2.2 a conformant actor object is a strict superset by construction, so the check as written
**encodes the deviation**: fix the install path and the check goes red; land a conformant template
wearing the actor label and the check goes red before the install path is touched at all. That is a
previously-green BLOCKING-ALWAYS suite going red, i.e. Halt 2, reached by doing the right thing.

The check is not being _weakened_ — the reshaped assertion is "equals the tier template ∪ the twenty
triples", which asserts strictly more and still fails both directions — so PROTOCOL §10.2 is not
engaged. But it is a check change motivated by an implementation defect, which Guardrail 9 puts in a
different unit from the fix. Three ordered sub-units, and the order is forced:

- **5b-0-i — reshape V-BRK-013 to read 06 §2.2 as well as §2.2.1.** Discover actor objects by label,
  determine the tier from `kube-agents/tier`, and require the rule set to equal the tier's §2.2
  template plus the §2.2.1 block, in both directions. The existing shared pair is recognized during
  the transition by the marker its own header already claims (tier-neutral, no `kube-agents/tier`)
  and asserted against §2.2.1 alone, so the check stays green on today's tree while describing
  tomorrow's. Needs the multi-line flow-sequence handling §2.2's blocks use and §2.2.1's do not —
  Prettier reflows long `resources: [...]` lists — so `_flow_items` gains a bracket-joining
  normalizer. No install change. L0.
- **5b-0-ii — render the conformant per-tier actor object.** Three templates
  (`actor-grant-{developer-team,cluster-admin,platform}.yaml.template`, following the existing
  per-tier template family), a `render_actor_grant <tier> <namespace> <leaf>`, the binding, and the
  wiring into `apply_agent_identity` — which already renders grant-then-identity in that order for
  the reason this needs too. Retire the shared pair rather than delete it. L0 for the equality, L2
  for the two-sided `auth can-i` that proves the scope actually changed.
- **5b-0-iii — re-run `broker-execute-l2.sh` past step 3** and score V-BRK-006 L2 and V-REV-001.

**New check ID.** None. This is V-BRK-013 doing what it was always for; a second check reading the
same spec section would be the two-copies-that-agree failure its own template warns about. Next free
V-BRK id is **V-BRK-033** if 5b-0-ii's L2 arm turns out to need one of its own.

**Not in this unit family.** `vap-agent-scope` (P10-T1) is §2.2's validator and may lag the grant —
precisely the state §2.2.1 has shipped in since P9-T7d-5.

### P9-T9b-5b-0 — outcome so far: two product defects fixed, and 5b-0-ii confirmed as the blocker

The plan above was written before any of it had been executed against a live broker. Three
`broker-execute-l2.sh` runs later it is neither superseded nor wrong — it is **vindicated and was
attempted too narrowly**. This section records what actually landed, the two product defects the
runs found, and why V-BRK-006 (L2) and V-REV-001 are still `deferred`.

#### What was tried instead of 5b-0-i/ii, and why it was not enough

`8182078` took the small road: it moved `agents [get,list,watch]` from the namespaced half of
`broker-operations-grant.yaml.template` to the tier-neutral `ClusterRole`, which is the single rule
the step-3 diagnosis above named. That is a real fix and it holds — `LowerTierOwner` now gets its
cluster-scoped `list agents` — but it treated a **class** of demotion as one instance of it. The
plan's own sentence "the same demotion applies to `actionrecords` and `approvalrosters`, which is
worth checking in the same unit rather than discovering one at a time" was the warning, and the
thing discovered one at a time turned out not to be in §2.2.1 at all.

#### Defect 4 — `ScopeOfTarget` built a malformed target scope for the platform tier

Fixed at `0e0ebf0`. Found by the second run, which got past the `agents` list and died one line
later with `503 snapshot-failed … target scope {ProjectID:your-gcp-project-id ClusterName:
Namespace:kubeagents-execute-tenant} is malformed`.

`ScopeOfTarget` built the target's scope by stamping the target's namespace onto the caller's
**authority** scope. 06 §1.2 gives the platform tier `projectId` and nothing else, so for a platform
caller that produced `{p, "", ns}` — an empty level above a non-empty one, which is precisely the
hole `scope.IsWellFormed` exists to reject and `scope.Contains` would read as a wildcard.
`resolveOwner` guards the live lookup with nothing but `live != nil`, so **the broker answered 503 at
step 3 for every namespaced operation a platform agent had ever submitted, on every cluster, since
the ownership lookup started reading live state.**

The missing datum is not authority — it is which cluster the target sits in, and a broker serves
exactly one. `Caller` gains `ServingCluster`, read from `spec.harness.clusterName` on the broker's
own Agent CR, and `ScopeOfTarget` fills the cluster from it when the caller's scope names none. Filled
**unconditionally**, for the same reason the namespace assignment is: the conditional form is the
gat-151 shape one line up, and here it would leave a platform agent's cluster-scoped writes ungated
against a cluster-admin owner.

Invisible below L2 by construction: the one platform-tier unit test that reached `Find` passed `""`
for the namespace, the single target shape that does not open the hole.
`TestTargetScopeIsWellFormedForEveryTier` now covers the cross product of three tiers × two target
shapes and the fail-closed direction; mutation verdict `caught`.

The fix does not move the blast-radius denominator (`CountWorkloadObjects` reads `s.Namespace` and
nothing else). Its only other behavioural delta is that a platform agent's cluster-scoped writes now
gate as `cross-tier-direct-operation` when a cluster-admin agent owns the cluster, which is what
06 §4.2 says and is strictly more gating, never less.

#### Defect 5 — the material-egress scan needs an authority only 06 §2.2 confers

The third run got past ownership and stopped at

```
403 target-forbidden — step 3: resolving 1 operations against live state: resolving secret digests:
listing Secrets in scope your-gcp-project-id// for the material-egress scan: secrets is forbidden:
User "system:serviceaccount:kubeagents-system:platform-your-gcp-project-id-actor" cannot list
resource "secrets" in API group "" at the cluster scope
```

**The product is conformant here and the identity is not.** 06 §4.2's `secret-material-egress`
builds its digest set from "every `Secret` **readable in the caller's scope**", and 06:1659 fixes
the granularity — "scoped to the caller's own namespace / cluster rather than the fleet". For a
platform caller that is the serving cluster, so a cluster-wide `list secrets` is the specified read.
`livestate.SecretDigests` fails **closed** on a denied List and is right to: treating Forbidden as an
empty digest set would report "no secret material" for every payload, and the actor's grant is not a
sound proxy for the reader SA's, so an unreadable Secret is not an unexfiltratable one.

06 §2.2's platform template grants exactly this — `- apiGroups: [""] resources: [namespaces,
serviceaccounts, configmaps, secrets] verbs: [get, list, watch, …]`, on a **ClusterRole**. It is not
in §2.2.1 and never was, so no narrowing of the shared pair reaches it. **Only 5b-0-ii lands it.**

**And the fixture path is closed, deliberately.** The obvious shortcut — widen the test-only overlay
— cannot be taken and should not be:

- V-CTN-037 (`check_test_only_grants_are_confined`, L0, BLOCKING-ALWAYS) fails any
  `kube-agents/test-only-grant` document that is cluster-scoped. A `ClusterRole` granting cluster-wide
  `list secrets` is exactly that. The check refuses the shortcut before review has to.
- `actor-tenant-write-grant.yaml`'s header already ruled on the narrower version: "`secrets` is
  absent and is the one omission worth stating … a test-only grant over Secrets on a shared scratch
  cluster is the one thing here that would be worth stealing."

So the blocker is not environmental and not a missing fixture. It is the install path owing the
platform actor an authority the spec gives it, which is 5b-0-ii, gated behind 5b-0-i by Guardrail 9.

#### The tier the suite drives, recorded as a question and not resolved here

`broker-execute-l2.sh` drives `examples/gitops-repo/fleet/platform-agent.yaml`, i.e. a **platform**
agent writing a ConfigMap into a tenant namespace — which is the operation 03 §3.2 says the platform
agent may not perform directly and 06 §4.2's `cross-tier-direct-operation` exists to gate. Driving
the suite as a developer-team caller would be more faithful **and** would put the digest-set list
inside one namespace, where a namespaced test-only Role can grant it within V-CTN-037's bounds.

It is not done here because V-6 (`validateParentCeiling`) requires a developer-team Agent CR to name
a live cluster-admin parent, which must in turn name a platform parent: the suite would seed a
three-agent chain, three actor identities and three broker deployments. That is a fixture reshape of
its own size, and it would also stop exercising defect 4's fix at L2 (a developer-team caller's scope
already names its cluster, so the `ServingCluster` fill never fires). Recorded as a carried finding
for 5b-0-ii to rule on once the grant exists, not as a way around the grant.

#### Verdicts

| ID            | Level | Verdict    | Blocker                                                                    |
| ------------- | ----- | ---------- | -------------------------------------------------------------------------- |
| **V-BRK-006** | L2    | `deferred` | P9-T9b-5b-0-ii — the platform actor holds no §2.2 grant, so step 3 refuses |
| **V-REV-001** | L2    | `deferred` | same                                                                       |
| **V-BRK-013** | L0    | `pass`     | unchanged and green; no check was touched in this unit                     |

The suite reports this as `DEFERRED: the envelope was not accepted, so no journal entry exists to
judge`, which is the correct shape — neither row is about admission, and a check that could not run
its property is never a pass.

### P9-T9b-5b-0-i — V-BRK-013 now reads 06 §2.2 as well as §2.2.1

The check had one definition site and one bound: every Role/ClusterRole labelled
`kube-agents/role: actor` holds exactly 06 §2.2.1's twenty broker-operations triples. That is true of
today's tree and it is the sentence that has to stop being true before 5b-0-ii can render anything —
so it is replaced here, in its own unit, per Guardrail 9.

It now reads **two** definition sites and joins them by tier. 06 §2.2 gives three per-tier ACTOR
templates — what an agent acts on, different for each tier — and §2.2.1 gives the grant every actor
identity _additionally_ receives, byte-identical across tiers. What an object is owed therefore
depends on whether it stamps itself with a `kube-agents/tier`:

- **tier-neutral** (no `kube-agents/tier`) — the shared broker-operations pair, owed §2.2.1 alone.
  That is all eleven actor objects in the tree today.
- **tier-stamped** — owed its tier's §2.2 template **∪** §2.2.1's grant.

Property 3 (nothing outside the bound) and property 4 (the bound is fully realised) are both
evaluated against that per-tier set. Property 4 stays a **union over the tier's objects** rather than
a per-object equality, because the grant is deliberately split across a ClusterRole and a Role —
cluster-scoped reads above, persisted writes below — and it is only demanded of a tier that has at
least one object. A tier with none has not been rendered yet, and a BLOCKING-ALWAYS check that goes
red until an unrelated future unit lands is not a deferral 09 §9.6 permits; it is a red gate.

Reading §2.2 at all required one parser change. Its `resources:` lists are long enough that Prettier
reflows them across lines, and §2.2.1's are not, which is why this never came up. `parse_rules` gains
`_join_flow_sequences`, a bracket-depth pass that collapses a reflowed flow sequence back onto one
logical line **before** the key/value reader sees it. Joining is done there, on physical lines, and
not by making the reader tolerant of newlines: a `resources:` with nothing after it is
indistinguishable from the start of a _block_ sequence until you have counted brackets, and quietly
reading a block sequence as empty is exactly the silently-smaller-grant failure `GrantSyntaxError`
exists to prevent. Depth only rises at a `[`, so a bare `resources:` still raises.

#### Two things the unit found that the plan did not have

**1. `k8s-operator/scripts/*.yaml.template` was outside the check's population.** `read_sources`
admitted `.yaml` and `.yaml.tmpl`. The provisioning path renders `broker-operations-grant.yaml.template`,
so the one copy of the grant that actually lands on a live cluster was the one copy nothing compared
to the spec — and it is also where 5b-0-ii's per-tier templates will live, which would have made the
whole tier arm describe a tree it could not see ([[LSN-036]], [[LSN-050]]). Fixed by extension, not
by path. The corpus goes from 6 actor objects to 11; both new ones are green.

**2. The spec permits the per-tier object and the shipped admission policy refuses it.** This is the
real find, and it is a constraint on 5b-0-ii that the plan did not carry.

`vap-agent-readonly` validation 3 bounds every actor object to a literal CEL allow-list under
`failurePolicy: Fail`, and property 2 has always asserted that allow-list equals §2.2.1's grant. Its
`isActor` variable selects on the actor label **alone**, with no tier condition — so it governs
tier-stamped objects too. A conformant platform actor ClusterRole carries 122 triples from 06 §2.2,
of which **116 are outside that allow-list**, `list secrets` among them. The API server would refuse
the apply. The spec says one thing, the installed policy says the other, and the two disagreeing
quietly is how a render lands that no cluster will ever accept.

06 §2.2 names `vap-agent-scope` as the validator for the tier templates and it does not exist
(P10-T1). Note that landing it does not by itself help: admission is conjunctive, so a second policy
admitting the tier rules leaves `vap-agent-readonly` still rejecting them. **The bound in
`vap-agent-readonly` itself has to move, in the same unit as the render.**

That is now **property 6** — no actor object grants a triple the installed allow-list rejects,
measured against the **intersection** across copies, because a rule admitted by one cluster's policy
and refused by another's is refused. It is green today (no tier-stamped object exists) and goes red
the moment 5b-0-ii renders one without moving the bound.

Property 6 reduces "admission would reject it" to "it is outside the allow-list" **only** while
`isActor` is the bare label test. Narrowing `isActor` is the most natural way to make property 6's
finding go away, it reads as a scoping fix, and it silently turns the reduction false. So the premise
is asserted rather than assumed: **property 7** compares the `isActor` expression, in every copy,
against the one form the reduction holds for.

#### What ran

| Artifact                                           | Result                                                                                                                 |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `actor-grant-single-sourced.py`                    | PASS — 20 grant triples; §2.2 adds cluster-admin=157, developer-team=152, platform=122; 3 VAP copies; 11 actor objects |
| `actor-grant-single-sourced.py --negative-control` | PASS — **14/14** (was 8/8), each caught by the property it targets                                                     |
| `dev/tests/invariants-gate.py`                     | 22/22                                                                                                                  |
| full `dev/L0-CHAIN.txt`                            | clean                                                                                                                  |

The six new mutations exist because the tier arm has no subject in the real tree, and an arm no input
reaches is unexercised prose ([[LSN-035]]). The control **synthesises** the object 5b-0-ii will
render — built _from_ the spec, not from a literal copied out of it, because a literal would be a
fourth definition site and that is the one thing this check exists to forbid — and then perturbs it:
conformant-but-inadmissible (property 6), one rule its template excludes (3), one rule its template
requires, dropped (4), a typo'd tier (the mis-stamp that the old check would have measured against
the wrong template and passed), a narrowed `isActor` (7), and a spec flow sequence swapped for a
block sequence (the joiner's own risk).

No mutant spec under `verification/mutants/`: V-BRK-013 predates `dev/mutate.py` and carries its
sweep in-tree as `--negative-control`, which is the same discipline — each mutation named, and caught
by the property it targets, not merely by the check going red.

#### Verdicts

| ID            | Level | Verdict | Note                                                                   |
| ------------- | ----- | ------- | ---------------------------------------------------------------------- |
| **V-BRK-013** | L0    | `pass`  | reshaped; strictly more asserted, 14/14 control, green on today's tree |

Unchanged and still blocked: **V-BRK-006** (L2) and **V-REV-001**, on 5b-0-ii. 5b-0-ii now owes three
things rather than two — the per-tier actor object, the binding, **and** the `vap-agent-readonly`
allow-list that admits it.

### P9-T9b-5b-0-ii-a — the phase profile, and the two shapes of the bound

5b-0-ii was planned as one unit: render the per-tier actor object, bind it, and (after 5b-0-i) widen
the allow-list that admits it. Planning it turned it again, and made it both smaller and different in
kind.

#### 06 §2.2's templates cannot be rendered whole inside Phase 9

The platform ACTOR ClusterRole at 06:714–760 grants `create`/`update`/`patch`/`delete` on
`namespaces, serviceaccounts, configmaps, secrets` **cluster-wide**. Phase 9's acceptance in 07 §2 is
that the whole safety machinery runs end to end **with no write authority anywhere** — actor SAs bound
to "empty" roles, everything dry-run. Rendering the template whole would hand every platform actor
cluster-wide `delete secrets` in the phase whose entire point is that nothing can write. That is not a
sizing problem; it breaks the phase's own acceptance criterion.

#### The spec already sequences the write half elsewhere

03 §4.2 (03:245–254) is decisive: `vap-agent-scope` is "**the same policy object as the read-only
generation's `vap-agent-readonly`, inverted**: reader SAs keep the read-verb allow-list; actor SAs get
a scope-and-template allow-list instead of a blanket write denial", and "'exceeds its tier template'
is decidable in CEL **only because the template is compiled into the policy as a literal allow-list,
generated from the same source as the rendered manifests**." That generator is **P10-T0**, deliberately
scheduled ahead of **P10-T1**'s flip. Hand-transcribing three per-tier literal allow-lists into YAML
in Phase 9 is exactly what B-002 refused, and would create a fourth definition site of the thing
V-BRK-013 exists to single-source.

So: **Phase 9 renders the READ half.** All four of `broker-execute-l2.sh` step 3's live reads
(`list secrets`, `get namespaces`, `list agents`, the per-kind counts) are reads, so the read profile
is sufficient to unblock V-BRK-006 (L2) and V-REV-001. The write half arrives with `vap-agent-scope`.

#### What admission then needs, and what it does not

Validation 3's inner test becomes `v in ['get','list','watch'] || (g+'/'+res+':'+v) in [ …20… ]`. No
new literal list, no `has(...)` carve-out (5a's ruling forbids those as "a hole in the one runtime
backstop, shaped exactly like a label that turns the policy off"), and `isActor` untouched. It
preserves 01:200's stated property exactly — the set of **write** verbs admissible to any agent
identity stays the four already enumerated — and it makes the actor arm say what validation 1 already
says about every other agent RBAC object. **03 §4.3's obligation table (03:277–287) assigns
`vap-agent-scope` only _write_ obligations for actors**; no row obliges admission to bound actor
_reads_ per tier, which is what licenses the disjunct.

#### Guardrail 9 forces the split, again

The check must learn the new shapes before the product carries them. **5b-0-ii-a** (this unit, check
only) → **5b-0-ii-b** (three read-only templates, `render_actor_grant`, the binding, the wiring into
`apply_agent_identity`, retire the shared pair, and the disjunct in all three VAP copies) →
**5b-0-iii** (L2).

#### What changed in V-BRK-013

**A ceiling and a profile, which are not the same set.** Property 3's bound becomes `ceiling(tier)` —
the whole of 06 §2.2 ∪ §2.2.1, write verbs and all, which does **not** move with the phase. Property
4's bound becomes `profile(tier)` — §2.2.1's grant plus the read verbs of §2.2's template while
`DARK_PROFILE` holds. Only the completeness direction moves. The asymmetry is guarded: a strict-subset
assertion plus a floor of 10 triples fails loudly if the read filter ever selects nothing away (dark
mode is over, say so) or eats the template (the filter is broken).

**Property 2 gained an assertion it never had.** The allow-list is only a bound in the company of the
CEL that consumes it, so validation 3's verb test is now pinned to exactly two shapes: `bare` (the
list alone) or `read-widened` (the list plus the three read verbs). A third shape is a failure. A
check that reads the list and shrugs at the expression would score a `v != 'delete' ||` prefix —
everything but one verb, admitted — as an unchanged twenty-triple policy.

**Property 6 became the mechanism rather than the intention.** Its bound is whichever shape _every_
copy carries, conjunctively. Under `read-widened`, a write triple from a tier's own template is
admitted by neither disjunct — so what holds Phase 9 dark is admission, and this check asserts
admission's shape rather than trusting the render to stay honest.

`DARK_PROFILE = True` is the one constant that flips, in the same unit that widens the allow-list from
`read-widened` to P10-T1's per-tier template allow-list. Flipping either alone turns the check red,
which is the intended coupling.

#### One hole the probe found, and closed

Verifying "green on the tree 5b-0-ii-b will produce" is not optional for a check reshaped ahead of its
implementation — a check that is green today and red on the correct future tree makes the next unit
look like it broke something. Probing it that way found a false negative: **the check would have
passed a `developer-team` ClusterRole**, which `vap-agent-readonly`'s wrong-scope validation denies
outright. 06 §2.2 gives all three tiers a template and says nothing about the **kind** that carries
it, so "render each tier's template" reads as a ClusterRole three times, is perfectly conformant to
§2.2, is admissible under a widened validation 3 — and is refused anyway. Property 6 now covers
validation 2 as well, with the namespace-scoped tier **parsed out of the policy**, not named in the
check: "developer-team is namespace-scoped" already has definition sites in 03 §4 and in the policy,
and a third one inside the check that exists to forbid third copies would be its own joke.

#### What ran

| Artifact                                           | Result                                                                                                          |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `actor-grant-single-sourced.py`                    | PASS — per tier, ceiling/profile: cluster-admin 171/83, developer-team 172/89, platform 136/68; 3 copies `bare` |
| `actor-grant-single-sourced.py --negative-control` | PASS — **20/20** (was 14/14), each caught by the property it targets                                            |
| the future-tree probe                              | 0 findings on the widened policy + three read-only profiles, developer-team as a `Role`                         |
| `dev/tests/invariants-gate.py`                     | 22/22                                                                                                           |
| `negative-controls-name-their-rule.py`             | PASS — 11 controls                                                                                              |
| full `dev/L0-CHAIN.txt`                            | clean                                                                                                           |

The six new mutations are the six wrong ways to land 5b-0-ii-b: the render without the widening (the
ordering constraint 5b-0-i discovered, restated as a mutation); the widening with a **write** verb in
the disjunct; a profile missing one read triple; the whole template rendered under a widened policy —
the row without which `DARK_PROFILE` would be a comment, since property 4 alone permits a superset;
the namespace-scoped tier as a ClusterRole; and a copy losing its wrong-scope validation.

#### Verdicts

| ID            | Level | Verdict | Note                                                                                     |
| ------------- | ----- | ------- | ---------------------------------------------------------------------------------------- |
| **V-BRK-013** | L0    | `pass`  | strictly more asserted; 20/20 control; green on today's tree **and** on 5b-0-ii-b's tree |

Still blocked on 5b-0-ii-b: **V-BRK-006** (L2) and **V-REV-001**.

---

### P9-T9b-5b-0-ii-a-fix — the phase arm reads both copies (halt cleared)

**Out of sequence, and it has to be.** This unit is not part of the 5b-0-ii-\* ladder; it exists
because PR #83's first CI run turned **V-BRK-023** (L1, **BLOCKING-ALWAYS**) red and a
BLOCKING-ALWAYS failure halts everything until a human clears it. The clearance came as
_"run the harness until completion, start with the failed PR #83"_ (2026-07-30). The halt is closed;
the ladder resumes at `5b-0-ii-b`, unchanged.

#### What was wrong, in the order it has to be read

Nothing about V-BRK-023's 2026-07-29 pass was untrue when it was written. `ActionRecord` carries
`+kubebuilder:subresource:status`, so `client.Create` drops the status block, so a freshly created
record's `status.phase` was empty **on the server** no matter what the caller set. The confirmer's
phase arm therefore read the `kube-agents/status` **label**, and said so at length, on the explicit
ground that reading `status.phase` "would have looked more correct and would have been vacuous".

`304c1d5` then made that premise false, correctly. 06 §4.3 makes `status.phase` **authoritative** and
the label a **derived index**; leaving status empty inverted the two, and every parked record read
back an empty phase behind a label claiming otherwise. `journal.Store.Create` now follows itself with
a `Status().Update`. `TestCreateDropsStatusAndKeepsThePhaseLabel` went red at that commit saying
exactly this, in a failure message that named the remedy — which is the whole reason it asserted a
premise rather than trusting one, and the reason the diagnosis cost one reading of the log instead of
a bisect.

**What the correction exposed is worse than a stale assertion, and is the actual finding.**
`journal.Store.SetPhase` writes status **first** and the label **second**, and documents the second
as _"best-effort ordering, never best-effort truth … the reconciler repairs the label if this second
write is lost"_. So there is a window — unbounded, if that write is lost — in which `status.phase` is
`Rejected` and the label still reads `Executing`. An arm reading the label alone reads the
**non-authoritative** copy and **admits** the action. A fail-open window inside a fail-closed arm,
in the last thing standing between a parked record and a live mutation.

#### The fix, and why (b) rather than (a) or (c)

The halt posed three options and named (b) as _"the only one that is strictly fail-closed in the
divergence window"_. `ConfirmDurable` now reads both copies and refuses in two steps:

1. **Divergence is a refusal in its own right.** When the authoritative copy and its index disagree
   there is no single answer to "what phase is this record in", and the write-ahead rule does not
   recognise _"probably Executing"_ any more than it recognises _"probably journaled"_. This is
   strictly more than option (a) would give: it fails closed on the `SetPhase` window in **both**
   directions, including the one where the label is ahead of status.
2. **Past that, the agreed phase must be `Executing` or unset.** Both-empty still confirms —
   `journal.Labels` omits the label when the phase is unset and `Create` returns before the status
   write, so "no phase at all" is a shape a caller can legitimately produce, and it is the
   two-empties case rather than a half-written record.

Choosing either copy alone trades one guarantee for the other. Requiring both costs one assertion.

#### Guardrail 9 and §10.2, argued rather than waived

- **§10.2** (narrowing a BLOCKING-ALWAYS check is itself a halt) does not engage: the arm refuses a
  strict superset of what it refused before.
- **Guardrail 9** (a check may not change in the same unit as the implementation whose failure
  motivated it) holds on three independent grounds. The replaced test exercises `journal.Store.Create`
  and **not** the confirmer, so changing the arm does nothing to make it green — the
  edit-the-check-to-green-the-implementation shape is structurally absent, not merely resisted. The
  implementation whose failure motivated the test change is `304c1d5`, a **prior committed unit**. And
  the assertion count goes **up**: 17 test functions / 30 cases → 19 / 38.

#### What landed in the tests

`TestCreateDropsStatusAndKeepsThePhaseLabel` is **replaced** by
`TestCreateWritesBothThePhaseAndItsLabel`, which measures the new ground truth against a real API
server and asserts three things: status carries the phase, the label carries it, and the two agree.
Added alongside it:

- `TestARecordWhoseStatusMovedWithoutItsLabelDoesNotConfirm` — reproduces the `SetPhase` window by
  issuing `Status().Update` **directly**, deliberately not through `SetPhase`, precisely so the label
  is left behind; asserts the half-written state really is on the server before confirming.
- `TestEveryPhaseSurvivesLabelEncodingUnchanged` — the divergence arm compares byte-for-byte, so a
  future phase name that `journal.labelValue` rewrites (a slash, a space, 64 bytes) would refuse
  **every action of that phase**. Discovered by asking what the new arm's inputs are, not by a
  failure.
- Six divergence subtests in `TestEveryRefusalIsNotDurable`, plus an `atPhase()` fixture helper that
  sets **both** copies, so the divergence arm and the phase arm can never cover for each other by
  accident.

#### Verdicts

| ID            | Level | Verdict | Note                                                                                                                |
| ------------- | ----- | ------- | ------------------------------------------------------------------------------------------------------------------- |
| **V-BRK-023** | L1    | `pass`  | halt cleared; 19 functions / 38 cases, 0 skipped; 100.0% of statements; 19/19 mutants caught; `results.csv` row 147 |

Non-vacuity is `verification/mutants/V-BRK-023.json`, committed here for the first time — the
2026-07-29 sweep for this ID ran through the superseded `dev/mutate.sh` and left nothing behind to
re-run. Its first key is a warning rather than a mechanism, and that gap is [[LSN-054]]: six of the
nineteen mutants are caught only by envtest tests and score **ESCAPED** if `KUBEBUILDER_ASSETS` is
unset, and `go test -list` — the guard [[lsn-048]] added so a missing catcher cannot read as a
survivor — compiles rather than runs, so it lists those six either way and stays silent.

#### Lessons opened

- **[[LSN-054]]** — an envtest suite `t.Skip`s itself when the assets are unset, so the package prints
  `ok` over a red test. Fifteen `*_envtest_test.go` files here behave identically. The sharp part is
  that **[[LSN-052]]'s own preferred mechanization, `go test ./...`, is green on this failure**: a
  mechanization that would not have caught the escape that motivated it is [[lsn-019]] arriving inside
  the fix for LSN-052. The corrected candidate names `make -C k8s-operator test`.
- **[[LSN-055]]** — twenty-five CHECKPOINT commits, one push, one CI run, and the red belonged to a
  commit twenty back. Also establishes that LSN-052's candidate 2 (a push trigger on non-`main`
  branches) is necessary but **insufficient**: a trigger nothing triggers is still one run.

---

### P9-T9b-5b-0-ii-b — the read half is rendered, and admission is widened to admit it

The other half of the split 5b-0-ii-a forced. That unit taught **V-BRK-013** the two shapes; this one
builds the tree the second shape describes: three per-tier actor grants carrying the READ half of
06 §2.2 joined with §2.2.1, their bindings, the renderer, the wiring, and the
`v in ['get', 'list', 'watch'] ||` disjunct in all three copies of `vap-agent-readonly`. One unit,
because either half alone is a red check — the render without the disjunct is an object no cluster
would accept, and the disjunct without the render widens a policy nothing exercises.

#### Three templates, and why they are not three copies of one shape

Property 4 of V-BRK-013 holds the **union of a tier's objects** to that tier's profile, and validation
2 of `vap-agent-readonly` denies a `ClusterRole` labelled `kube-agents/tier: developer-team`. Those
two facts together fix the shape, and it is not uniform:

| tier             | objects                                                                 | why                                                                                                                                                                                                                                                                                                   |
| ---------------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `developer-team` | one namespaced `Role` + `RoleBinding`, 89 triples                       | a namespace-scoped tier may not own a ClusterRole. Its three cluster-scoped grant rules are written out **inert** — a `Role` cannot grant `tokenreviews` — so the tier's union still equals its profile, and the tier-neutral `kubeagents-broker-operations` ClusterRole is what grants them for real |
| `cluster-admin`  | `ClusterRole` (83 − journal) + `Role` (journal, roster) + both bindings | binding one per-tier ClusterRole cluster-wide would grant `create actionrecords` in **every** namespace, which is the fleet-wide-writer shape `agent-identity.yaml.template` already forbids and V-BRK-012 catches one layer down                                                                     |
| `platform`       | same two-object split, 68 triples                                       | as above                                                                                                                                                                                                                                                                                              |

Arithmetic, hand-checked against 06 §2.2 before anything was written and then confirmed by the check:
developer-team 69 read + 20 grant = **89**; cluster-admin 69 + 20 − 6 overlap = **83**; platform
54 + 20 − 6 = **68**. Exactly the numbers 5b-0-ii-a's profile computation printed a unit earlier.

#### "Retire the shared pair" turned out to mean retiring one half of it

The plan said retire the shared pair. Only the **namespaced** `Role kubeagents-broker-operations`
could go: every triple of it now lands in a per-tier `Role`, stamped, and an unstamped shared object
belongs to every tier and is therefore evidence for none of them under property 4. The **ClusterRole**
survives, and the argument is written into its header rather than left implicit — `developer-team`
cannot own a cluster-scoped object, so its `tokenreviews`, `fleetfreezes` and `changepolicies` can
only come from an object wearing no tier label at all, and 06 §4.4 says a tier that cannot read
`fleetfreezes` fails closed permanently. Dropping it would not be a narrowing; it would brick a tier.

#### V-CMP-007 chose the renderer's shape

`render_actor_grant <tier> <namespace> <leaf>` selects its template with a `case` over three
**literal** filenames rather than interpolating `actor-grant-${tier}.yaml.template`. That is not
style: V-CMP-007 property 1 requires every `*.yaml.template` basename under `k8s-operator/scripts/`
to appear literally in text the install path executes, and a dynamic path would have made all three
new templates read as files nothing renders — an install-path hole with a green check over it.

`apply_agent_identity` now applies three streams in order (shared grant → tier grant → identity) in
both its dry-run and live arms; `delete_agent_identity` removes all four new per-tier objects, whose
names are a pure function of tier and leaf and would otherwise be inherited by the next install under
the same name. `dev/lib/agent-fixtures.sh` renders the tier grant too — without it an L2 fixture would
lose the journal `Role` entirely and the broker would fail step 11 with a 403 that looks like a bug in
the broker.

#### What the disjunct gave up, and what still holds

Validation 3 now admits any read verb. 03 §4.3's obligation table assigns `vap-agent-scope` only
_write_ obligations for actors, which is what licenses it; the read half is bounded instead by
V-BRK-013's per-tier union equality, and by P10-T1 moving that bound into the cluster. One thing is
genuinely conceded: a wildcard **group or resource** carrying only read verbs now passes admission,
which 06 §2.2's platform template requires. A wildcard **verb** does not — `*` is not a read verb and
no triple in the allow-list ends in `:*`.

One negative fixture became a positive. `vap_actor_negatives.yaml` DOC 4 (`secrets` get/list/watch on
an actor Role) asserted that the actor arm is a triple allow-list and not a write filter. That was
right about the policy and wrong about the spec it enforces, so it moved to `vap_actor_positive.yaml`
as DOC 5, with the flip and its replacement bound argued in the header rather than deleted — reverting
the disjunct now fails a fixture that says which side of the change came back. The remaining docs
renumber 5–8 → 4–7; none of them changes verdict.

#### The far side of the two-trees discipline

5b-0-ii-a committed negative-control rows that **synthesised** the tree this unit would build
([[LSN-053]]). Landing it inverts them, and the inversion is where the interesting failure was: two
rows scored `caught` for the wrong reason and one stopped firing entirely, because property 4 measures
a **union** and a lamed synthetic beside a correct shipped template is still a complete union. Both
missing-rule rows now perturb `actor-grant-platform.yaml.template` itself. The synthesis helper stays
for the rows that need a **wrongly** rendered object — the whole template, a rule outside it, a
mis-stamped tier, a namespace-scoped tier as a ClusterRole — since the shipped tree deliberately
contains none of those. Row count unchanged at 20, no signal relaxed, and the two rows that existed
only to describe the future tree became reachable regressions: the disjunct reverted in every copy,
and the disjunct reverted in **one** copy while the others stay widened.

#### What ran

| Artifact                                           | Result                                                                                                                                        |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `actor-grant-single-sourced.py`                    | PASS — tier-stamped actor objects present for **all three** tiers, each union exactly its profile (83 / 89 / 68), 3 VAP copies `read-widened` |
| `actor-grant-single-sourced.py --negative-control` | PASS — **20/20**, each caught by the property it targets                                                                                      |
| `identity-has-install-path.py` (+ control)         | PASS — every roleRef resolves, 15 manifest-emitting functions reachable; **8/8** controls fire                                                |
| `render_actor_grant` smoke                         | all three tiers render, names derived, no unsubstituted `${…}`, unknown tier exits non-zero                                                   |
| full `dev/L0-CHAIN.txt`                            | clean (274 lines)                                                                                                                             |
| `make -C k8s-operator test`                        | green, every package; `internal/controller` 127.0s                                                                                            |
| `make validate`, shellcheck, prettier              | clean over the whole `origin/main...HEAD` set                                                                                                 |

#### Verdicts

| ID            | Level | Verdict | Note                                                                          |
| ------------- | ----- | ------- | ----------------------------------------------------------------------------- |
| **V-BRK-013** | L0    | `pass`  | the tier arm now runs against the real tree, not a synthetic; 20/20 control   |
| **V-CMP-007** | L0    | `pass`  | three new templates on the install path, every roleRef resolving; 8/8 control |

Now unblocked and owed by **5b-0-iii** (L2): **V-BRK-006** and **V-REV-001**. `broker-execute-l2.sh`
stopped at step 3 because the platform actor could not list Secrets; it can now, on a cluster
provisioned from this tree.

---

## P9-T9b-5b-0-iii — the L2 arm, and the three defects standing behind it

**Landed 2026-07-31.** Commits `0fbe744`, `ce87423`. Branch `phase-9-actor-read-half`.

### What was asked, and what it turned out to require

The unit was one line of the ledger: re-run `broker-execute-l2.sh` past step 3 and score V-BRK-006
and V-REV-001. Step 3 was indeed unblocked by 5b-0-ii-b's read grant. Four separate things then had
to be true before the suite could return a verdict about the broker rather than about itself.

**1 — Step 11 returned HTTP 500: `cannot update resource "actionrecords"`.** Diagnosed as an
implementation defect and explicitly **not** a §8.5 spec contradiction. 06 §2.2.1 grants the broker
`actionrecords get list watch create` plus `actionrecords/status get update patch`, and withholds
`update` and `delete` on the resource itself deliberately — "the broker appends and advances
`status`; it can never rewrite or remove a record". `journal.SetPhase` syncs a derived label index
alongside the authoritative `status.phase`, and its own comment already promised that sync was
best-effort. The code returned the error. So every terminal transition the broker took reported a
500 for an action that had **already executed and already been journaled** — a false negative in the
audit trail. Fixed by tolerating `IsForbidden` narrowly (a `Conflict` or a 500 is still returned);
`JournalReconciler.repairStatusLabel` runs in the operator, which holds `update`, and repairs the
index on the next reconcile. Confirmed live: the record read back with the label repaired.

**2 — L2-1 could not have passed against any commit.** It asked the API server for the record by raw
action id; object names are `journal.RecordName` = `"ar-" + lower(actionID)` (06 §4.3). The arm had
never been measured, because the only thing exercising it was `--negative-control`, which synthesises
the record document. Judged **not** a Guardrail 9 violation and argued in the ledger's decisions
table: no implementation of this unit motivated the change, the arm could not have passed against any
tree, and the fix makes an inoperative arm operative — the assertion count rises and nothing narrows.
[[LSN-060]].

**3 — `status.timestamps` had three readers and no writer.** Declared since Phase 5; read by
`budget.go`'s window, `cooldown.go`'s `(verified, submitted)` pair and
`JournalReconciler.exportLateness`'s four-way fallback, all of which degrade silently. V-BRK-006's L2
clause compares `metadata.creationTimestamp` against `status.timestamps.executionStarted`, so its
evidence had never existed. The pipeline now stamps five beats: `submitted` and `classified` on the
write-ahead Create, `classified` taken at the **end** of step 4 so a submission refused as
`forbidden` never claims it was classified; `executionStarted`, `executionEnded` and `verified` off
the wall clock and never off `s.at`, which is frozen before step 3 and would fabricate a violation.
`approved` stays nil — it belongs to the ChatOps gateway SA.

**4 — and none of it would have reached etcd.** `SetPhase` re-read the record and wrote the _live_
copy, discarding `status.applied`, `status.verification`, `status.recovery` and the clock; and
`Store.Create` restored only `status.phase` after the API server's subresource drop, so the birth
beats were never durable **and** the caller's copy came back nil — which panicked the broker at step
8 on the first live run, between "the journal says `Executing`" and "anything has executed".
`mergeOwnedStatus` carries exactly the six fields 06 §4.3 assigns the owning broker SA, nil-guarded
per field, used by both `Create` (snapshotted _before_ the call, since the reply overwrites) and
`SetPhase`; `state.clock()` makes the stamping nil-safe. [[LSN-061]].

### Evidence

| What                                                 | Result                                                                                                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dev/verify/actor-overlay-admission-l2.sh`           | `P9-T9b-5a ruling: HOLDS` (chain order: it must precede the execute suite — the executor's step-8/9 apply uses `client.DryRunAll`)                                  |
| `dev/verify/broker-execute-l2.sh`                    | **rc 0 · 10/10 assertions · PROVEN**. L2-2: created `06:43:23Z` (API server), execution began `06:43:23Z` (broker). L2-4: `strategy 'delete', validated, 1 step(s)` |
| `dev/verify/broker-execute-l2.sh --negative-control` | **13/13**, run before and after every edit to the suite                                                                                                             |
| `verification/mutants/V-BRK-006.json`                | **15/15 caught**. M9 escaped the first sweep and was closed with a new test rather than a strengthened old one                                                      |
| `make -C k8s-operator test`                          | green, every package                                                                                                                                                |
| `dev/L0-CHAIN.txt`                                   | green, 386 dev tests + 30 invariant checks                                                                                                                          |

Images: operator `@sha256:69bedbec93b9`, broker `@sha256:85532a853384`, both `dev-ce87423`, deployed
by digest via `dev/cluster/reload-images.sh`.

### What this unit did **not** do

- **V-REV-001 is n=1** — one single-target create. The wider `n` and the `undo <id>` round trip
  (V-REV-002) are Phase 10.
- **The write half stays dark.** Only the READ half of 06 §2.2 is rendered; `vap-agent-scope` arrives
  with P10-T1. `actor-overlay.sh`'s four write negatives remain absolute for that reason.
- **`status.approvals` is still written by nobody**, because its principal — the ChatOps gateway SA —
  does not exist yet. `mergeOwnedStatus` is written to leave it alone when it does.

---

## P9-T9b-5b-ii-a — V-BRK-018 at L2, and the two objects that were not the grant

**Landed 2026-07-31.** `dev/verify/broker-refuse-l2.sh` is **14/14, rc 0, PROVEN** on
`gke-scratch-kube-agents-dev`. V-BRK-018 is scored `pass` at L2 and so is the journal half of
Phase 9 acceptance (d). Two rows in `verification/results.csv`.

### The split, recorded at SELECT

`T9b-5b-ii` carried three subjects, the same way `T9b-5b` carried three before it: a snapshot-failure
refusal, a journal-unavailable refusal, and V-BRK-021's surface scan. The first two are the same
fixture with a different fault and read back through the same journal; the third is a scan of the
served HTTP surface and shares nothing with them but the pod. So:

| Unit            | What it is                                                                                             | Checks                                  | Level |
| --------------- | ------------------------------------------------------------------------------------------------------ | --------------------------------------- | ----- |
| **T9b-5b-ii-a** | `broker-refuse-l2.sh` — two refusals: a split snapshot and a journal revoked out from under the broker | V-BRK-018 (L2), Accept (d) journal half | L2    |
| **T9b-5b-ii-b** | V-REV-003 (no generatable undo plan ⇒ reclassified gated) and V-BRK-021's L2 surface scan              | V-REV-003, V-BRK-021 (L2)               | L2    |

### How V-BRK-018 is made non-vacuous under shadow mode

The row says a snapshot-persist failure must refuse and leave **neither target applied**. Everything
in Phase 9 is a server-side dry run, so neither target was ever going to exist and the two NotFounds
that look like the assertion prove nothing. The property is carried by the **journal** instead:

- a submission that gets **past** step 3 leaves a write-ahead record naming both real targets, with
  captured pre-state;
- a submission stopped **at** step 3 leaves exactly one record — `rejection.go`'s — whose
  `spec.targets` is the single `refused-before-target-resolution` sentinel, with no `spec.preState`
  and no `status.applied`.

Those two worlds are distinguishable in the API server, which is where A-6 asserts. A-7's NotFound
pair stays as the cheap direct half and does not carry the row.

Finding the record needed a second decision: a refusal reply carries **no `actionId`**, so there is
no handle to look one up by. The suite lists ActionRecords and matches `spec.trace.traceId`, which
`rejection.go`'s `traceFromBody` copies off the caller's own envelope — and **A-2 asserts the
missing `actionId`**, so the method's own premise is under test rather than assumed.

### The fault that would not stage

Scenario B revokes `actionrecords` from the actor while the broker is running. The first
implementation stripped the two objects `actor-grant-platform.yaml.template` renders — the actor
ClusterRole and the per-tier namespaced Role — and the authorizer still said `yes` after 60s. The
suite deferred, correctly, rather than submitting into a healthy journal and reporting a 503 that was
never coming.

The cluster also carried `Role/kubeagents-broker-operations` and
`RoleBinding/platform-agent-broker-operations` in `kubeagents-system`, granting the same verbs.
**No current template renders either** — they are residue from before the split moved the namespaced
verbs onto the per-tier Role, and `kubectl apply` does not delete what it stopped rendering. So the
strip is now **discovery-based**: walk the actor's ClusterRoleBindings and RoleBindings, take every
`roleRef` that carries `actionrecords`, snapshot each object, strip it. Three objects on this
cluster; zero would be a deferral, because nothing to revoke is no fault to stage. Restore is the
snapshots **and** a re-seed, in that order — a re-seed alone re-renders only the shipped objects, so
an unowned object stripped here would have stayed stripped.

Filed as a backlog finding rather than fixed here: the residue itself is a provisioning-lifecycle
problem (a `teardown_NN` reaps what its own `provision_NN` created, never what a previous generation
of the template created), and the live install should be **checked** for the same objects.

### `verify-phase9.sh` section E was a false pass

The Accept (d) arm tested `[ -f dev/verify/broker-execute-l2.sh ]` and, on finding it, reported the
journal half proven — on the strength of a comment claiming that file "carries the journal-unavailable
refusal", which it does not and never did. [[LSN-060]]'s shape through a different door: an artifact
detector that names the wrong artifact is a detector of nothing. It now requires the file to exist,
to appear in a live line of `dev/L2-CHAIN.txt`, **and** runs it.

Retargeting a detector in the same unit that builds the artifact it detects is **Guardrail 9
adjacent and deliberately on the right side of it**: the old arm passed unconditionally, the new one
can fail, and no assertion was weakened to make anything green.

### What this unit does not claim

Row 3's **auto-pause**. `brake.go` sets `AutoPause: true` and the 503 the caller receives says "and
the agent is being paused", but nothing consumes the field and `status.broker.journalReachable` has
no writer. Row 9's auto-pause is wired, which is what makes this a gap rather than unbuilt scope.
Backlog finding, 2026-07-31; not asserted by any arm here.

---

## P9-T9b-5b-ii-b-1 — V-REV-003 at L2: the third outcome, and the control that gives it meaning

**Landed 2026-07-31.** `dev/verify/broker-gate-l2.sh` is **16/16, rc 0, PROVEN** on
`gke-scratch-kube-agents-dev`; the negative control is **39/39**. **V-REV-003** is scored `pass` at
L2 — one row in `verification/results.csv`. `dev/L2-CHAIN.txt` gains a line and both L2 ratchet
floors move in the same commit (`L2_CHAIN_FLOOR` 19→20, `L2_SCOPE_FLOOR` 28→29).

### The ii-b split, recorded at SELECT

`T9b-5b-ii-b` carried two subjects and they share almost nothing. V-BRK-021 is a scan of the served
HTTP surface and needs only a driver pod with credentials. V-REV-003 is fault injection and needs a
tenant namespace, a write overlay, a shadow-mode patch on the Agent CR and a two-scenario probe. A
unit that built both would spend its fixture budget twice over on a shared pod. So:

| Unit              | What it is                                                                                              | Checks    |
| ----------------- | ------------------------------------------------------------------------------------------------------- | --------- |
| **T9b-5b-ii-b-1** | `broker-gate-l2.sh` — an envelope with no generatable undo plan, against a control whose plan generates | V-REV-003 |
| **T9b-5b-ii-b-2** | V-BRK-021's L2 surface scan, added to `broker-auth-l2.sh`, **plus** retargeting `verify-phase9.sh` §G   | V-BRK-021 |

`ii-b-2` owns the section G retarget because that detector is a **false pass today**: its only match
in the tree is `broker-refuse-l2.sh`'s "does not claim" note about V-BRK-021, so it reports a
claimant that does not exist. There is no V-REV-003 detector in section G at all, so this unit had
no Guardrail-9 entanglement — nothing here changes a check to make anything green.

### Two submissions, because the row is a difference

A broker that gated **everything** satisfies V-REV-003's sentence perfectly and is worthless, and
that is not hypothetical. Three places in this pipeline downgrade a plan to `none` on an error —
`checkRecreatable` when the reference index cannot answer, `undo.Validate` with no dry-run client
wired, `generateOne` on a missing snapshot. Each fails closed, correctly, and each would have made a
one-submission suite green while proving nothing about reclassification. So there are two:

- **F**, `patch` an `apps/v1 Deployment` **that does not exist**. `undo.StrategyFor` maps `patch` to
  `restore` for either existence; `execute.capture` narrows the NotFound to `Existed: false` with a
  nil pre-state; `generateOne`'s restore arm refuses on exactly that. Gated at step 7.
- **C**, `apply` an absent ConfigMap. Its inverse is a `delete` step, `PlanDryRunner` treats a delete
  step's NotFound as "would apply", the plan validates, the action is accepted and shadow-executed.

**D-1 asserts the difference itself**, as its own arm rather than as an inference a reader makes from
F-1 and C-1 sitting near each other. The two failures a per-scenario arm cannot see are "everything
is gated" and "nothing is".

### Three design choices that cost something

**The fault is a Deployment, not a ConfigMap.** `classify/floor.go`'s `statefulKinds` contains
`{Group:"", Kind:"ConfigMap"}`, so any ConfigMap operation able to produce this refusal would also be
gated by `RuleDestructiveStatefulDelete` or a neighbour. A gate with two independent causes cannot
attribute itself to either, and **F-5** — the arm that reads `spec.classification.reasons[]` and
requires `no-undo-plan` among them — is the one that would have gone quiet. The patch body is one
annotation for the same reason: a patch touching `securityContext`, `serviceAccountName` or a
pod-security label draws `RuleSecurityLoosen` in alongside it. (The live run confirms the concern is
real: the record's reasons are `no-undo-plan novel-action undo-plan-unusable` — three rules on an
envelope chosen to minimise them.)

**The control differs in verb and kind, which is a real cost.** The tightest control would be the
same `patch` over a Deployment that _does_ exist — one variable. Rejected on the code: that plan's
step is a server-side apply of the captured pre-state under the **agent's** field manager, over an
object this suite created with `kubectl apply` and therefore owned by a different one.
`PlanDryRunner.dryRunApply` passes no force flag, so a field-ownership conflict would downgrade the
plan and gate the control — for an artifact of how the fixture was made. A control that can gate for
a reason unrelated to the experiment is worse than one that differs in verb. `apply` an absent
ConfigMap is borrowed from `broker_execute_probe` instead: the one operation already **proven** to
reach the accepting path on this cluster, which is the point of it being a control.

**The envelope sends `dryRun: false`, and this is the unit's one sharp edge.** 06 §4.2 step 6's rule
is `UndoPlanGateApplies(dryRun, present) = !dryRun && !present` — **a dry run suppresses the
no-undo-plan gate**, deliberately, and `pipeline.go` step 4 feeds it the envelope's own value rather
than the effective one for that reason. A `dryRun: true` submission would still come back gated, via
the brake's row 5 (`BrakeRuleUndoPlanUnusable`), and the suite would be scoring V-REV-003 on a rule
03 §4.1 does not name. Sending false also makes "never auto-executes" a real request: the caller is
asking the broker to execute, and the only thing between the ask and a write is the gate.

What keeps it safe is **structural, not a promise**. The suite patches
`spec.operations.dryRunOnly: true` onto the Agent CR and **reads it back from the API server** before
either submission; `mayExecute = !env.DryRun && !shadowed(view)` is a one-way composition no caller
can clear. A read-back that disagreed would have been rc 3 with nothing submitted. Because the broker
starts from a CR carrying no `spec.operations` at all, the suite also waits **three brake cache TTLs
(15s)** so the first envelope cannot be answered from a view filled before the patch landed. Phase 9's
shape — "no write authority anywhere; the broker runs every action in dry-run" (07 §2) — is preserved.

### What the suite states it does not claim

- **The fault target's continued absence is over-determined.** Shadow mode alone produces it, and so
  does the gate, and the two are indistinguishable from outside. "Never auto-executes" is carried by
  **F-6** — no `status.applied`, no `spec.preState` — which is a claim about how far the _pipeline_
  got and is false for any broker that reached step 8. F-7 is kept as the cheap direct half because a
  gated action that created its target is a live safety defect worth a line.
- **The approval path is not exercised.** This parks an action and leaves it parked. 06 §4.3's
  surface needs an ApprovalRoster with a decision in it — Phase 10, V-REV-004.
- **`status.operations.dryRunOnly` is not read, because nothing writes it.** `OperationsStatus`
  exists in the API type and no controller populates it — the same gap `status.broker.journalReachable`
  has. The read-back is against `spec`, which is also what `pipeline.shadowed` consults.
- **06 §4.4 row 5 is not separately scored.** Both paths raise the same envelope and both reasons land
  on the record; F-5 asserts the classifier's rule by name rather than the brake's absence.

### P1 refused the first run, correctly

The first live attempt failed at P1: the deployed operator was `dev-0ea4235-dirty`, two commits
behind a tree at `9cb7465`. Neither commit touched Go — the binary was almost certainly identical —
and P1 refused to guess anyway, which is the whole design. `dev/cluster/reload-images.sh all` rebuilt
and deployed by digest; the scored run is at `dev-9cb7465-dirty-1785488736` on **both** pods.

### A finding, not this unit's

`kubeagents-router` is in `CrashLoopBackOff` on the scratch cluster —
`missing required --project-id / KAGE_PROJECT_ID` — and has been since before this unit: every prior
ReplicaSet is `0/0 created`. It is scratch-cluster configuration drift, not a regression this unit
caused and not something any L2 chain line asserts. Recorded in the ledger's findings; it needs a
ruling before the Phase 9 milestone, because a milestone run on a cluster with a dead router should
say so out loud rather than not notice.

---

## P9-T9b-5b-ii-b-2 — V-BRK-021 at L2: the surface of the binary the controller handed out

**Landed 2026-07-31.** `dev/verify/broker-auth-l2.sh` is **21/21, rc 0, PROVEN** on
`gke-scratch-kube-agents-dev` (up from 14/14); the negative control is **20/20**. **V-BRK-021** is
scored `pass` at L2 — one row in `verification/results.csv`. No new chain line: the suite that grew
the scan was already a live line of `dev/L2-CHAIN.txt` and its `--negative-control` was already a
live line of `dev/L0-CHAIN.txt`, so both floors stand.

### Why this is not the L0 half again

V-BRK-021's L0 half went green on 2026-07-30 (`P9-T7c-2c`, `results.csv` row 138), reshaped from a
route COUNT into four derived properties of `Server.MutatingRoutes()`. That row's own note says what
it does not settle, and 09 §6 gives the check **L0 and L2** rather than either alone. Three of the
clause's properties are structurally unreachable from `go test`:

- **A build-tag-guarded skip path is not in the binary the test builds.** `go test` compiles without
  the tag, so a `//go:build !prod` bypass is invisible to every assertion in the package — including
  the ones that read the route table.
- **A listener that is not `internal/broker` is invisible to a scan of `internal/broker`.** A sidecar,
  a profiler left in the base image, a debug server started from `main` — none of them appear in a
  source property of the mux.
- **05 §1.3's two future doors can only be shown empty on a deployed server.** `/v1alpha1/approve`
  and `/v1alpha1/replay` are named in the route table as Phase 10 work. "The population is empty,
  recorded as empty and never as satisfied" is a statement about what answers on the wire.

So the L2 half asserts **the same properties of a different subject**: the image the controller
handed out, at the digest P1 pinned, behind the real TLS listener, across the real Service and the
real NetworkPolicy.

### Where it was built, and why not somewhere new

Extended `dev/verify/fixtures/broker_probe.py` and `dev/verify/broker-auth-l2.sh` rather than adding
a suite. The probe already holds a mesh certificate, an audience-bound projected token and the
`hostAliases` pin that makes the SAN resolve without a DNS rule; a second driver pod would re-derive
all three to ask a cheaper question. `broker_probe.py` had exactly one consumer, so there was no
second caller to keep in step.

**Every scan request carries the agent's own good certificate and own good token.** That is the whole
reason a 404 is attributable to the ROUTE SET rather than to the caller — an unauthenticated probe of
an unknown path gets a refusal either way and cannot tell you which.

### The seven arms

| Arm | What it reads                                                                                  |
| --- | ---------------------------------------------------------------------------------------------- |
| a   | 19 non-routes — L0's 17 plus 05 §1.3's two future doors — all 404 `no-such-route`              |
| b   | 8 methods — five on the actions route, three on the nonce route — all 405 `method-not-allowed` |
| c   | 3 query parameters, all 400 `unsupported-query-parameter`                                      |
| d   | all 10 of `server.go`'s `bypassHeaders`, 400 `bypass-key`, plus one on the mutating route      |
| e   | the differential: the same route, same credentials, no header → 200 with an empty reason       |
| f   | 8 ports dialled from inside the cluster; exactly one accepts                                   |
| g   | container, Service and EndpointSlice each declare one port, and it is the same one             |

Three of these are load-bearing in ways worth writing down.

**(c) carries a real envelope.** Each query request submits an envelope built by the shipped builder
with a fresh nonce. A `{}` body comes back 400 `invalid-envelope`, and an arm that read only the
status could not tell the two apart — it would score a broker that ignores query strings entirely as
a broker that refuses them. `pretty=true` is in the list for the same reason: an innocuous parameter
is what separates an allowlist of zero from a denylist of the scary ones.

**(d) presents no Authorization header at all**, on `/healthz`. A 400 in that condition can only have
come from `ServeHTTP` ahead of the mux, which is where `rejectBypassHeaders` runs — so the arm is a
property of the server rather than of one handler. The count is an **equality**, not a floor:
`bypassHeaders` mirrors 06 §4.1's reserved body keys, and a scan of nine of them is a scan with a
hole in a place the design enumerates.

**(e) exists because (d) alone is satisfied by a broken broker.** Eleven refusals is also what a
broker that 400s everything produces, including one whose health route has failed.

### What the suite states it does not claim

- **(f) is a reachability claim, and is written as one.** A port nothing listens on and a port the
  `<agent>-to-broker` egress policy drops both arrive at the driver pod as "did not connect". Reading
  the timeout as proof that no process is bound would be reading the NetworkPolicy's verdict as the
  binary's. The claim is reachability from where an agent stands — which is the property
  non-skippability actually needs — and **(g)** covers the other side from three independent
  API-server writers rather than pretending the dial did.
- **Nothing decompiles the image.** What is asserted is the observable consequence of a skip path on
  the digest P1 pinned, not its absence from the object code.

### The section G retarget — the third false pass in one file

§G's V-BRK-021 detector was `grep -l 'V-BRK-021' dev/verify/*-l2.sh | head -1`, plus a check that the
match is a live `L2-CHAIN.txt` line. The tree's one match was `broker-refuse-l2.sh` — in a comment
recording that it does **not** carry the property ("→ P9-T9b-5b-ii-b, with V-BRK-021's L2 surface
scan"), and that file is a live chain line. Both halves of the test were satisfied by a note about
the absence of the thing under test.

This is the same shape as the Accept (d) arm (retargeted 2026-07-31, one unit earlier) and the
guard-1 arm (rewritten when it matched a function name and "was right by accident"). Three arms in
one file, three different doors, one defect: **a detector aimed at a NAME is indistinguishable, in
its own output, from a detector that is satisfied.**

The replacement discovers by the **refusal vocabulary the shipped server answers with**, each of the
four strings resolved out of `k8s-operator/internal/broker/*.go` from the code path that emits it —
`http.StatusNotFound`'s `Response`, `http.StatusMethodNotAllowed`'s, the block guarded by
`len(r.URL.Query()) > 0`, and `ReasonBypassKey`'s value. Rename a reason in the server without
renaming it in the suite and the arm fails, rather than quietly unhooking and going green on a suite
that now matches nothing. A claimant must also read both port outcomes in the same function body,
bound its own size against a floor, be called from somewhere, be a live `L2-CHAIN.txt` line, **and**
have its `--negative-control` be a live `L0-CHAIN.txt` line — both trees, as a gate rather than as a
habit.

**Guardrail 9 does not apply**, and it was checked before the edit rather than after: the detector
was a false pass on the pre-unit tree, so retargeting it closes a known false pass instead of editing
a check to make an implementation green. `ii-b-1`'s own split table assigned the retarget here.

**Shown non-vacuous twice.** Against the real pre-unit tree — `git stash` of the two changed files,
detector declines with "no live line of dev/L2-CHAIN.txt runs a suite that scans the deployed
surface" — and against four clause mutants under `dev/mutate.sh`, **4/4 caught**: the port outcomes,
every count floor, the ¬ chain line, and a server-side reason rename.

One of those four escaped on its first run and the **mutant** was wrong, not the detector: it deleted
one of five floors, and the clause only ever claimed the scan bounds its size _somewhere_ — a regex
over one function body cannot attribute a floor to a dimension. Strengthening the arm until that
mutant died would have been writing a claim to fit a test. The mutant was corrected to remove every
count comparison, and the bound is now stated in the arm's own comment.

### A note on `/bin/bash` 3.2

The first draft put the detector in `verdict="$(python3 - <<'PY' … PY)"`. macOS ships bash 3.2, which
mis-parses a heredoc nested inside a command substitution and reports the entire 600-line file as an
unterminated quote — `bash -n` fails at EOF and names no useful line. The verdict now travels through
a file and the pass/fail stays in the exit status, which is also the shape the guard-1 arm below it
already used.

---

## P9-T9b-5c — V-BRK-013 at L2: Accept (e)'s two-sided sweep, asked of the authorizer

**Landed 2026-07-31.** `dev/verify/actor-grant-sweep-l2.sh` is **13/13, rc 0, PROVEN** on
`gke-scratch-kube-agents-dev`; the negative control is **16/16**. **V-BRK-013** is scored `pass` at
L2 — one row in `verification/results.csv`. Three new chain lines: the suite itself on
`dev/L2-CHAIN.txt` (floor 20 → 21, scope floor 29 → 30) and both `--negative-control` and
`actor_grant_expectations.py --self-test` on `dev/L0-CHAIN.txt` (now 45 lines).

Phase 9 acceptance **(e)** — "no agent identity in the fleet holds a write verb, verified by a full
two-sided `auth can-i` sweep" — is the bullet this closes.

### Why the L0 half was a real result and still could not answer this

V-BRK-013's L0 half went green 2026-07-28 (`results.csv` row 82) via
`dev/tests/actor-grant-single-sourced.py`. It reads files, and three things follow from that which
only an authorizer can settle:

- **An RBAC object that omits `kube-agents/role: actor` is invisible to it and fully effective.**
  That label is the L0 check's discovery key — because it is also `vap-agent-readonly`'s actor
  selector — and RBAC is a union, so an unlabelled Role grants exactly as much as a labelled one.
  This is the suite's `¬`, and it is why the `¬` had to be on a cluster.
- **A rule written correctly and never applied reads identically at L0.** V-BRK-012's L0 half was
  green for weeks with no broker deployed anywhere.
- **No file holds the union across every binding**, and the union is the only thing that authorizes
  a request.

### Two-sided, because `verify-phase9.sh` §F already said so in its own failure text

A one-sided "no identity holds a write verb" sweep passes perfectly against a fleet whose RBAC never
applied — every answer is `no`, including the ones that should not be. And 06 §4.4 makes the failure
asymmetric in the other direction too: a _missing_ `fleetfreezes` read does not fail safe, it bricks
a tier permanently, because the tier can no longer see the freeze it is supposed to respect. So the
sweep asserts **204 grants held** and **434 verbs refused** in the same transcript, over the same
identities, and A-7 requires each reader to hold at least one read so that its denials are denials
rather than absence.

### The questions are derived, not listed

`actor_grant_expectations.py` — the L0 check's own parser of 06 §2.2 and §2.2.1 — emits the question
table, so there is one parser of the grant in the repository and phase-9.md's "asserts the exclusion
set **by name** rather than by 'these are the ones that were there when I wrote it'" holds
mechanically. 647 questions were asked. Not asked, and both counted and printed rather than dropped:
**121** wildcard rows (a wildcard _request_ is not the question the rule makes) and **55** rows
naming types this API server does not serve (KCC, `gateway.networking`), excluded from **both**
directions so the negative half cannot be padded by types nobody could grant.

### The fixtures seed the readers too, and that is a claim about what is measured

The first live run went red on A-7 for the platform reader, and the arm was right: `platform-agent-explorer`
was absent from the cluster, because it ships only under `examples/gitops-repo/policy/rbac-overlay/`
and the `clusters/cluster-a/…` apply path never reaches there. Roughly sixty of that identity's
`reader-no-write` rows were passing vacuously. The fixture now applies all three tiers' explorer
grants out of the shipped overlays — extracting only the `-explorer` documents, so the actor
ServiceAccount in those same files, which carries a literal `PROJECT_ID` placeholder, is never
applied. A sweep whose answer depends on which directory somebody applied in July is not measuring
the repository.

### Three defects this unit's own machinery had

- **`IFS=$'\t' read` collapses runs of tabs.** Tab is an IFS _whitespace_ character, so every query
  row with an empty `subresource` column parsed shifted left by one field, and the sweep asked
  `auth can-i own fleetfreezes --subresource=get -n ''`. That answers a clean `no`, and a `no` is an
  ordinary thing for this suite to record: the only visible symptom was three tiers apparently unable
  to read `fleetfreezes` — **a defect wearing a finding's clothes**, and it cost a full live run.
  Fixed by splitting on `\x1f`, which is not IFS whitespace. The analyzer now also rejects any column
  holding a value outside its own alphabet, because a field-shifted row has the right _number_ of
  columns and the wrong values in them, and `field-shifted-parse` is `¬` case 15.
- **`cluster-check-hygiene.py` property 1b (LSN-044) failed the script on arrival.** Hoisting the
  query into `ask_one` makes the resource word computed, which is exactly the refactor that lesson
  names as the evasion of 1a's static ban on a literal slash. The landing spot is the expensive one:
  434 of the 647 questions are negative, and a slashed word answers a confident `no` about an object
  _named_ `status`. The remedy is `resource_word`, a **named** function rather than an inline `case`
  so `--negative-control` can fire it without a cluster — a `*/*)` arm that never triggers reads
  exactly like one whose pattern is wrong. It is `¬` case 16, and the only one that exercises a
  function the live path calls.
- **The `¬`'s decoy staged a fault nobody asked about.** The first decoy Role granted
  `create deployments.apps`, and no row of cluster-admin's derived table asks that, so one of the two
  planted grants could not flip an answer. The decoy's rules are now derived from the victim's own
  query rows, and S-4 requires both to flip.

### What the suite states it does not claim

Wildcard rows, unservable types, and anything about agent-process behaviour: `auth can-i` answers for
a subject, not for the code that runs as it. The `NEGATIVE CONTROL DOES NOT EXERCISE:` block
(LSN-060) names the derivation, the `can-i` invocation itself, identity discovery, the served-type
filter, P1 and the fixtures — everything upstream of the assertion block, which the `¬` synthesises
past. S-4 is what covers the first three, and it is live-mode only: a decoy grant is a statement
about an authorizer, and there is no authorizer at L0.

### P9-T8b-4b-ii-2b-ii splits: 2b-ii-a is the corpus, 2b-ii-b is the soak

**Sized at SELECT, 2026-07-31, and it is a session and a half.** A new L2 suite, a new corpus probe,
a derived envelope corpus, a `--negative-control`, target seeding, journal mining over the executed
population, guard 3 restated as a label assertion, and both chains rewired — the same magnitude as
`P9-T9b-5c`, which took a whole session and produced one suite. Carrying it forward whole is the
shape PROTOCOL §2 names: an oversized unit is not finished late, it is checkpointed half-done.

| Unit                     | What                                                                                                                                                                                                                                                                                       | Checks              | Level |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- | ----- |
| **P9-T8b-4b-ii-2b-ii-a** | The soak **corpus**: derive the executable envelope population from `verification/fixtures/classifier-corpus.yaml`, filtered by what `dev/verify/fixtures/actor-tenant-write-grant.yaml` authorizes, with a `--self-test` carrying vacuity floors and negative arms. Hermetic, no cluster. | feeds **V-REV-001** | L0    |
| **P9-T8b-4b-ii-2b-ii-b** | The **soak**: a corpus probe, `dev/verify/undo-coverage-l2.sh`, target seeding, journal mining over the `DryRun` population, guard 3 as a label assertion                                                                                                                                  | **V-REV-001**       | L2    |

**The split is free, and that is a fact about P1 rather than a judgement.** `_p1_build_inputs()`
(`dev/lib/preconditions.sh:62`) maps freshness per image: `k8s-operator | kage-router | kage-broker`
resolve to the `k8s-operator` build context and everything else returns 1. A commit touching only
`dev/`, `verification/` and `docs/` therefore does not invalidate the running broker's P1, so 2b-ii-b
inherits 2b-ii-a's tree without a rebuild. Splitting a unit that _did_ touch the operator would cost
a `cloud-build-push` between the halves; this one costs nothing.

**Why the corpus is a unit and not a helper file.** It is the exact parallel of
`actor_grant_expectations.py` preceding `actor-grant-sweep-l2.sh`, and three things live in it that
have already gone wrong in this phase:

- **It is where the population that makes V-REV-001 non-vacuous is established.** The phase has
  reached an empty or n=1 population twice — `broker-execute-l2.sh` claims V-REV-001 today with
  **n=1**, and its own `results.csv` note says so. A coverage check over one record is a check whose
  denominator is a rounding artifact. 09 §11.11 keeps V-REV-001 and V-REV-002 apart "precisely
  because the first is cheap and reassuring and the second is the one that matters"; a cheap check
  over n=1 is not even reassuring.
- **It is where LSN-036's headcount risk lives.** A corpus that is _derived_ can silently shrink to
  nothing when its input moves; a corpus that is _listed_ goes stale without saying so. Deriving it
  is the right call — the grant is shipped, the classifier corpus is spec-bound at 120–200 cases —
  but a derivation with no floor under it is the worse of the two failures, because it presents an
  empty result as a clean run.
- **It is where the filter can quietly become a hardcoded kind list.** The authorized set must be
  read out of the write overlay, subresource granularity included (`deployments/scale` is a separate
  rule from `deployments`), so that narrowing the grant narrows the corpus. A hand-listed
  `{ConfigMap, Deployment}` passes every test on the day it is written and stops tracking the grant
  the first time the grant moves.

**The one thing the corpus must NOT do.** It must not assert that the live broker's classification
equals `expect.class`. The classifier corpus is a source of **envelopes**, not of expected classes:
its cases are already-resolved inputs scored against the offline classifier, while the live broker
classifies against production labels, live state and the seen/novel history of a real cluster. An
equality assertion there would be a second V-MET-005 wearing V-REV-001's ID, and it would go red for
reasons that say nothing about undo coverage. `expect.class` is used for exactly one thing: as the
selection filter for the **non-gated** classes, because those are the population 09 §6.3 scopes the
check to. What the soak reads back is the class the broker actually chose.

### P9-T8b-4b-ii-2b-ii-a — outcome, 2026-07-31

**V-REV-001's denominator went from 1 to 37, and the number is derived rather than written down.**
`dev/verify/fixtures/soak_corpus.py` reads `verification/fixtures/classifier-corpus.yaml` (181
cases), filters it by what `dev/verify/fixtures/actor-tenant-write-grant.yaml` actually authorizes,
and emits the envelope population the soak will submit: **37 cases, 4 verbs, 2 kinds, both non-gated
classes, both seed states.** `--self-test` is **17/17** and is a new `dev/L0-CHAIN.txt` line (45 →
46). No `results.csv` row: this unit scores no check. It builds the population `2b-ii-b` will claim
**V-REV-001** over, and a verdict row for a fixture would be a pass with no property behind it.

#### The reject table is the deliverable as much as the selection is

144 of the 181 cases are excluded, and every one of them carries exactly one reason from a **closed**
set, with `selected + rejected == total` asserted:

| reason                | n   | why it is not in the soak                                                                |
| --------------------- | --- | ---------------------------------------------------------------------------------------- |
| `class-gated`         | 80  | 09 §6.3 scopes V-REV-001 to the non-gated population; a gated action does not execute    |
| `class-forbidden`     | 26  | never reaches the planner                                                                |
| `not-authorized`      | 24  | the write overlay grants configmaps, deployments and deployments/scale, and nothing else |
| `abort`               | 4   | the envelope is refused before classification                                            |
| `class-unstated`      | 4   | the case asserts rules only; there is no class to filter on                              |
| `multi-op`            | 3   | one target per record, because the seeding and the journal mining are both per-target    |
| `unnamed-target`      | 2   | the executor snapshots by ref, and a ref with no name is not one                         |
| `verb-not-executable` | 1   | `cloud` is not in the envelope schema's `VALID_OPS`                                      |

A **derived** corpus fails the opposite way to a listed one. A listed corpus goes stale loudly; a
derived one shrinks to nothing silently and prints the empty result as a clean run. The histogram is
what makes a shrink visible as a reason that moved rather than as a smaller number with no
explanation — and the arithmetic assertion is what stops a case being dropped by neither path.

#### The floors are fired against an empty selection, not only against today's tree

`floor_problems()` is a pure function of (selected, rejected, total, grant) precisely so the
self-test can call it twice: once on the real derivation, which must return nothing, and once on a
**synthesised empty selection**, which must return at least five complaints. A floor that has never
been observed to fire is a floor whose threshold is a guess, and the collision floor is the case in
point — nothing in the real corpus produces two cases with one target name, so without the synthetic
arm that branch would ship unexecuted. Same for `MIN_SELECTED = 20`: it is set below the measured 37
on purpose, because 09 §7.1 lets the classifier corpus move between 120 and 200 cases and a floor
pinned to today's exact yield fails on somebody else's legitimate edit. Low enough not to be brittle,
high enough that **n=1 — the thing this unit exists to fix — cannot pass**.

#### The filter reads the grant, and there are three arms that prove it does

The tempting shape is a hardcoded `{ConfigMap, Deployment}`. It passes every test the day it is
written and stops tracking the grant the first time the grant moves. So the authorized set is read
out of the overlay file, and the self-test mutates that file in memory:

- Revoke `deployments` → all **33** non-scale Deployment cases drop, the ConfigMap cases are
  untouched, **and the scale case stands**, because it rides on the separate `deployments/scale`
  rule. An arm that expected the scale case to disappear would be asserting that the reader ignores
  subresources.
- Revoke `deployments/scale` → the scale case drops and `patch Deployment` stands.
- Make every verb `get/list/watch` → **every** previously-selected case comes back rejected, and
  by the name `not-authorized` rather than something vaguer.

The RBAC verbs each envelope verb needs are read off what the executor's client actually calls
(`execute/client.go`), not off the verb's name: `apply`/`create` go through a server-side-apply
`Patch` and therefore need **patch + create**, and `scale` is an `Update` on the **`scale`
subresource**. `get` is deliberately absent from the requirement — every op snapshots its pre-state,
but that read belongs to the READ overlay, and asking the write overlay for it would reject every
case for the wrong reason.

#### The grant reader refuses what it does not understand

`dev/tests/yamlsubset.py` cannot read the overlay and should not: it rejects flow collections by
design, and the overlay is written in them because it is an RBAC manifest a human applies, not a
corpus prettier reformats. So `load_grant` is a narrow three-line-form reader — and its one
load-bearing property is that a rule it does **not** understand is an **error**. It counts
`- apiGroups:` lines and requires that number to equal the number of triples it matched. A
silently-skipped rule narrows the authorized set, which narrows the corpus, which shrinks
V-REV-001's denominator: the exact failure this module exists to prevent, arriving through its own
input reader.

#### The one assertion this file must never make

**It does not assert that the live class equals `expect.class`.** The corpus cases are inputs already
resolved against a fixture world; the live broker classifies against production namespace labels,
live object state and a real cluster's seen/novel history — and every selected case is re-addressed
to the one tenant namespace the suite owns, which can by itself move a case across the production
ladder. An equality assertion there would be a second V-MET-005 wearing V-REV-001's ID, going red for
reasons that say nothing about undo coverage. `expect.class` does exactly one job: select the
non-gated classes. **`2b-ii-b` reads back the class the broker chose and partitions on that.**

`KIND_TO_RESOURCE` holds the one remaining hand-written mapping, and it is a naming convention rather
than a policy statement — a Kind missing from it is rejected as `not-authorized`, the conservative
answer. The silent-shrink hole is in the other direction, so a floor closes it: **every resource the
grant names must be reachable from some Kind in the map**, which means adding `ingresses` to the
overlay fails this file until it learns `Ingress`.

### P9-T8b-4b-ii-2b-ii-b — outcome, 2026-07-31

**V-REV-001 now has a denominator of 35 instead of 1, and the 100% means something it did not mean
yesterday.** `dev/verify/undo-coverage-l2.sh` submits the whole derived corpus — **37 envelopes, one
per row of `soak_corpus.py --table`** — from inside the cluster on the platform agent's own reader
identity, through the broker's real front door, all dry-run. **35/35 = 100%** of executed non-gated
`ActionRecord`s carry a validated undo plan, across **3 verbs**. `--negative-control` is **17/17**.
Both chains gained a line (L0 46 → 47, L2 21 → 22) and both floors moved in this commit.

Live run, `gke-scratch-kube-agents-dev`, rc 0, 9/9 assertions, banner PROVEN, on
`k8s-operator@sha256:69bedbec93b9` and `kage-broker@sha256:85532a853384` with both P1 arms green:

| arm     | property                           | result                                              |
| ------- | ---------------------------------- | --------------------------------------------------- |
| A-1     | submission floor                   | 37 of 37 accepted (floor 20)                        |
| A-2     | every accepted action is journaled | 37/37 mined as `ar-<lowercase actionId>`            |
| **A-3** | **V-REV-001**                      | **35/35 = 100%**, all four failure categories zero  |
| A-4     | non-vacuity                        | 35 records, 3 verbs — above the 20/2 floors         |
| A-5     | attribution                        | 37/37 attributed; 30 of the namespace's 67 excluded |
| A-6     | the shadow held                    | all 37 seeded targets byte-identical                |

#### The two records that are not in the denominator are the classifier working

35, not 37. The missing pair is `gat-005` and `gat-100`, both `delete Deployment`, both
`PendingApproval`, class **gated**, strategy `none`. A gated action never executes, so it never gets
a `status.timestamps.executionStarted`, so it is correctly outside a claim scoped to _executed_
records. That is the shape the suite has to be able to distinguish from the failure that looks
identical from one field away — a record that executed and got `none` anyway — which is why
`strategy-none-on-non-gated` is its own FAIL category rather than folded into "no plan".

#### "Executed" is the broker's own stamp, not a guessed-at set of phase strings

`status.timestamps.executionStarted != ""` is the moment the broker issued its first mutating call.
The alternative — enumerating the phase values that mean executed — is a list that has to be kept in
step with a state machine in the product, and the failure mode is that a renamed phase silently
empties the denominator and prints 100% of nothing. Hence the DEFER arms: an empty executed
population, a submission count below the accept floor, or a single-verb population all exit **3**
with a named blocker. Three of the 17 `¬` cases assert exactly those, because a floor that has never
been observed to fire is a guess.

#### Four failure categories, scored separately, one of them derived from the product's own comment

`no-undo-plan`, `strategy-none-on-non-gated`, `unvalidated-plan`, `stepless-plan` — plus
`unclassified-executed-record`, which **fails** rather than quietly leaving the denominator. The
second one is not a rule this suite invented: `undo.StrategyFor`'s closing comment is _"anything
else ⇒ none ⇒ gated"_, so `none` on a record the classifier did **not** gate is the planner and the
classifier disagreeing about the same action. A suite reporting one aggregate number could say a
plan was missing but not which of the four ways it was missing, and those are four different bugs in
four different places.

#### Guard 3 could not be a label, and what replaced it is stronger

The plan said _"guard 3 as a label assertion"_ — tag this run's records and mine by the tag. It is
not available. `pipeline.chainID(env, actionID)` returns `undoOf(env)` or the actionId and **ignores
the envelope's own `trigger.chainId`** for non-undo actions, and `journal.Labels()` writes only tier,
scope, risk-class, trigger, chain-id, status and undo-of — **nothing a submitter chose**. So a
suite cannot label a broker-written record at all.

A-5 is set intersection on actionIds instead, and it names the failure better than the label would
have. The tenant namespace is this suite's own and it is deliberately left standing between runs
([[LSN-045]] — a namespace holding an `ActionRecord` is undeletable), so the journal accumulates:
the run that produced this row found **67** records in `kubeagents-system` and put **30** of them
outside the denominator by name. Without that arm the denominator grows every run and a stale 100%
reads like coverage improving.

#### The corpus supplies shape and target; the payload is synthesised

A soak that replayed recorded bodies would be measuring the fixture. `undo_coverage_probe.py` takes
the row's verb, kind and target from the corpus and **builds** the operation: a ConfigMap or
Deployment desired-state for `create`/`apply`, a deterministic three-way rotation for `patch`, a
replica count for `scale`. Deriving patch bodies from the corpus's `touchedPaths` was rejected —
only 3 of the 37 rows carry one, and `gat-066`'s is the immutable `/spec/selector`. One op per
envelope, one object per op, because both the seeding and the journal mining are per-target.

Each envelope fetches its **own** nonce; they are single-use (`broker/antireplay.go`), so a shared
one would have made the run a 1-accept, 36-replay-refusal transcript. A failed case emits its row
and continues rather than aborting, so one bad envelope costs one row of the denominator instead of
the whole population — and A-1's floor is what stops that degradation from passing quietly.

#### A-6 exists because a dry-run soak that wrote would still score V-REV-001 green

Everything above is a claim about records. None of it would notice the broker actually mutating the
cluster. The suite snapshots all 37 seeded targets with two bulk reads before the run and two after,
and fails on any create, mutation or delete — the same shadow property Phase 9's whole premise
rests on (07 §2: the entire safety machinery exercised **with no write authority anywhere**).

#### What this does not claim

The plans are read, never **executed**. Correctness of an undo plan is V-REV-002 and the approval
round trip is V-REV-004, both Phase 10; 09 §11.11 separates them from this one _"precisely because
the first is cheap and reassuring and the second is the one that matters"_. This row is the cheap
and reassuring one, and it is now cheap and reassuring about 35 records instead of one. Nor does the
gated pair's exclusion assert that gating a `delete Deployment` is correct — `broker-gate-l2.sh`
owns that property. And the suite never compares the live class to the corpus's `expect.class`, for
the reason `2b-ii-a`'s section gives: every case is re-addressed to one tenant namespace, which can
by itself move a case across the production ladder.

#### One thing worth carrying forward: P1's commit half is HEAD-exact

The first live run failed P1 with the deployed tag at `dev-0ea4235-dirty-*` against a tree at
`cdad3b1`, and `git log 0ea4235..HEAD -- k8s-operator/` showed **zero** commits. That is not a bug.
`_p1_build_inputs()` scopes only the **dirty-file** half of the check; `_p1_assert_tag_is_current`
compares the deployed sha against `git rev-parse --short HEAD` and fails on any mismatch at all. So
a commit touching nothing but `docs/` and `dev/` invalidates every image for every L2 suite still to
come — which is the phase's own ordering rule ("L0/L1 before L2, because the tree must stop moving
before images are built") stated from the other side. Remedied as documented, with
`reload-images.sh operator` then `broker`, and both arms went green on the re-run.

---

### P9-T9c-1 — outcome, 2026-07-31

**06 §4.4 row 3's second clause, wired.** The row is one sentence with an AND in it — _"Refuse to
execute; set `status.broker.journalReachable: false`; auto-pause"_ — and only the first clause was
true. `brake.go` set `AutoPause: true` on the row-3 decision, `pipeline.stepBrake` captured `d.Effect`
and dropped the rest, and the caller was told _"and the agent is being paused"_ by a broker that
paused nothing (B-006).

**Where the pause goes, and why it is not a `client.Patch` on the Agent.** The broker has
`get, list, watch` on `agents` and nothing more (06 §2.2.1, asserted by V-BRK-013, BLOCKING-ALWAYS),
because 05 §1.7 wants "exactly one code path that stops an agent". So a pause is a _request_ written
onto an `ActionRecord`'s `status.escalation`, which C-BR fans out into `spec.operations.paused` plus
an Event. `internal/broker/escalate` already existed and row 9 already drove it; this unit connected
the other caller.

**The one structural problem, and its answer.** Row 3 fires at pipeline step 5. The action's own
record is not durable until step 8. There is therefore no action record to hang the escalation on —
so it rides the **refusal** record `server.refuse()` already writes via `ref.Journal`, which names
the same agent and puts the refusal and the pause request on one object. `RejectionJournal.Reject`
now returns the action id it minted; without it the boundary would have to re-derive a ULID it did
not generate.

| What changed                        | Where                                                                                           |
| ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| `Refusal.AutoPause`                 | `envelope.go` — carried like `Journal`/`SecurityEvent` are                                      |
| `Decide` copies it onto the refusal | `brake.go` — one definition site; the decision is consumed locally, the refusal is what travels |
| `Reject` returns the action id      | `rejection.go` — interface, Store impl, log impl                                                |
| `Config.Pauser`, required           | `server.go` — `verify.Pauser`, the same type row 9 uses                                         |
| `refuse()` consumes it              | `server.go` — `autoPause`, after journaling, never before                                       |
| Wiring                              | `cmd/broker/main.go` — `&escalate.Recorder{Client: k8s, ...}`                                   |

**What this half does NOT fix, and it is the reason for `-2`.** Row 3 fires when the brake's journal
probe says the store is unreachable, and a store that cannot be listed usually cannot be written —
so in the common case the refusal is not recorded, there is no id, and the pause request has nowhere
to live. It is logged and the agent stays live. What it _does_ catch is the case that is most likely
in practice: the probe is a periodic observation, so it can read unreachable for up to one interval
after writes have recovered, and in that window the record lands and the agent is paused. The case
it cannot catch is exactly why 06 §4.4 also asks for `status.broker.journalReachable` — a surface
that does not depend on the journal being writable. That is `-2`.

**Non-vacuity: 4 mutants, 4 caught, each by the arm that targets it** ([[LSN-035]]). Run through
`dev/mutate.sh` — the one-off layer — rather than `dev/mutate.py`, because a sweep spec is keyed by
check ID and 09 has no ID for row 3's pause yet (that question travels to the improvement pass with
B-006).

| Mutant                                              | Caught by                                                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `Decide` stops copying `AutoPause` onto the refusal | `TestBrakeEachRuleFiresInIsolation` (both row-3 arms) + `TestRow3RefusalAlsoRequestsTheAutoPause` |
| `refuse()` never consumes `AutoPause`               | `TestRow3RefusalAlsoRequestsTheAutoPause` — `pause requests = 0, want 1`                          |
| Every refusal auto-pauses                           | `TestAnOrdinaryRefusalDoesNotPause` — a bypass-key refusal is not a fleet incident                |
| The pause is hung on an empty action id             | `TestRow3WithNoJournaledRecordCannotPause`                                                        |

The second mutant is worth noting for the next person: the first form of it deleted the whole `if`
block, which left `recordedActionID` declared-and-not-used, so Go refused to compile and the mutant
scored as `BROKEN` rather than caught — the check was never asked anything ([[LSN-048]]). Rewritten
as `if false && ref.AutoPause`, it compiles, runs, and fails the intended assertion.

**The tests drive the real row-3 refusal**, built by `Decide(healthy())` with the journal signal
removed, not a hand-written `Refusal`. A hand-written one would keep passing if the brake stopped
setting the field, which is the regression most worth catching: the brake and the boundary have to
agree about what row 3 _is_, and only one of them is under test at the boundary.

**Verification.** `make -C k8s-operator test` green including the 116 s controller envtest; L0 chain
**47/47**; invariants gate 30/30 after winding `dev/assertion-baseline.json` for the three new named
tests (LSN-056). No check ID claimed — see the note in the task section above.

### P9-T9c-2 — outcome, 2026-07-31

**Row 3's middle clause, and the design question `-2` was split out to answer.** The question was
_who writes `status.broker.journalReachable`_, and reading 06 §2.2.1 and §2.2 together answers it
closed: the broker-operations grant is `[get, list, watch]` on `agents` and nothing else; the actor
templates add `update, patch` on `agents` for the platform and cluster-admin tiers — for provisioning
their **children** — and **nothing anywhere grants `agents/status`**, which is a subresource and
therefore a separate grant ([[LSN-061]]). developer-team has no `agents` verb at all. No broker at
any tier can write this field. The operator is the only principal that can, so the writer is a
controller.

**The objection that stood for five phases, and why it does not apply.** `agent_controller.go` has
carried a comment since Phase 4 saying the controller "cannot observe" journal reachability. That is
an argument against **one transport** — asking the broker over HTTP, where a broker answering "yes"
proves nothing about the broker's own writes — not against the controller observing anything. What
makes a controller-side observation evidence is 05 §1.2: the journal store is not a service, it is
the `ActionRecord` CRD in the cluster's own etcd. _"For a Cluster Admin or Developer Team Agent the
journal lives in the same etcd as the objects it describes"_; for the platform tier it lives in the
hub cluster's etcd, which is where the operator runs. The store the broker probes and the store the
operator probes are the same store, always.

**Three observations, conjoined, every unknown false.**

1. The broker Deployment is ready — already computed by `updateStatusReady`. This also covers total
   loss of the broker's API path, because `brake.NewSource`'s startup `Refresh` is synchronous: a
   broker that cannot read the API server dies at boot, not on the first envelope.
2. An **uncached** `List` of `ActionRecord` in the agent's namespace, `Limit(1)` — deliberately the
   same shape as `brake.Source.probe`. Uncached because a `List` served from an informer is the false
   green `brake.MaxFreezeStaleness` already argues against; the nil-reader case fails closed rather
   than falling back to the cached client.
3. A `SubjectAccessReview` asking whether the actor ServiceAccount may `create actionrecords` there.
   **This is the one that stops the whole thing being a proxy** — it is not the operator's
   connectivity restated, it is the API server's authoritative answer about the _broker's_ authority,
   obtained by the one principal that may ask.

| What changed                                    | Where                                                                              |
| ----------------------------------------------- | ---------------------------------------------------------------------------------- |
| The probe, and ~90 lines on why it lives here   | `journal_reachability.go` — new                                                    |
| `APIReader` + `Authorizer` on the reconciler    | `agent_controller.go` — defaulted in `SetupWithManager`, fail-closed when nil      |
| `JournalReachable` written into `BrokerStatus`  | `agent_controller.go` — `updateStatusReady`; the reason is logged on the EDGE only |
| Periodic requeue, `brokerHealthRequeue` = 60 s  | `agent_controller.go` — the one status field with **no watch behind it**           |
| `subjectaccessreviews: create`                  | `config/rbac/role.yaml`, regenerated — +6 lines, no other drift                    |
| `journal_reachability.go` classified: RENDERING | `dev/tests/pause-is-not-scale-to-zero.py` — see below                              |

**Why the clock.** A journal outage changes no object the controller owns or watches, so
`journalReachable` is the only field in `updateStatusReady` that goes stale silently. The periodic
requeue broke `agent_controller_test.go`'s `RequeueAfter != 0` assertion; rather than weaken it, it
was **split into two stronger ones** — "not the 30 s degraded retry" (the original property, restated
precisely, which `!= 0` only implied) and "is the broker-health period". Guardrail 9 forbids editing
a check to accommodate an implementation; strengthening it to say what it meant is the other
direction.

**The check that caught the new file, and the latch it forecloses.** V-RUN-012 fails any unclassified
file in `internal/controller`, which is the arm that stops the guard silently narrowing as the
package grows — so the new file had to be declared. It landed in **RENDERING**, the strict arm, even
though it renders no workload, because it is the one file where reading the brake would _close a
latch_: row 3 auto-pauses on an unreachable journal, so a probe that also consulted `paused` ("a
paused agent is not executing, don't bother probing") would report unreachable **because** it had
been paused, and the pause would outlive the outage with no path back.

**Non-vacuity: 8 mutants, 8 caught, each by the arm that targets it** ([[LSN-035]]). Through
`dev/mutate.sh` for the same reason as `-1` — 09 has no check ID for row 3 yet.

| Mutant                                           | Caught by                                                             |
| ------------------------------------------------ | --------------------------------------------------------------------- |
| `JournalReachable` dropped from `newBroker`      | `TestJournalReachableReachesAgentStatus/the_broker_may_write...`      |
| Return true before the `SubjectAccessReview`     | 3 tests; `reviews issued = 0, want exactly 1`                         |
| A failing `List` returns reachable               | `.../the_store_does_not_answer`                                       |
| Nil dependencies return reachable                | all 3 subtests of `TestJournalProbeWithNoDependenciesWiredIs...`      |
| `Resource: "actionrecords/status"` ([[LSN-044]]) | `review resource = .../actionrecords/status, want .../actionrecords`  |
| The three ServiceAccount `Groups` dropped        | `review groups [] are missing "system:serviceaccounts"` ×3            |
| The periodic requeue removed                     | `TestAgentReconciler_Reconcile_ExistingRuntimeClass`                  |
| The probe reads `.Spec.Operations.Paused`        | V-RUN-012 property 3 — both the `.Spec.Operations` and `.Paused` arms |

The fifth is worth keeping: it is LSN-044's exact shape, and the test that caught it is the one that
pins the _question_ rather than the answer. `wantUser` in that test is a **literal**
(`system:serviceaccount:team-x:developer-team-team-x-actor`) with a separate assertion that the
fixture still produces it — built from `actorServiceAccountName(agent)` it would have been a value
compared against itself, which can never report the wrong shape ([[LSN-034]]).

**The recorded residue.** A broker whose pod-level network path to the API server breaks _after_ boot
while its pod stays Ready. Its probes are `tcpSocket` on its own mTLS listener (which demands a
client certificate the kubelet does not have), and a bound port says nothing about egress. In that
state the broker knows and the operator does not. Closing it needs a transport from the broker to
something the broker may write — and that list is exactly `actionrecords` and `actionrecords/status`,
which _are_ the surface that is down. A heartbeat record would put a fourth kind of thing in an
append-only audit trail whose contents are evidence. Rejected transports are enumerated in the file
header so nobody re-derives them.

**Verification.** `make -C k8s-operator test` green including the 125 s controller envtest, and
`reap-envtest` reported nothing orphaned (B-004's fix, working); L0 chain **47/47** including
V-RUN-012 and its negative control (8/8 breakages caught); invariants gate **30/30** after winding
`dev/assertion-baseline.json` for the five new named tests (LSN-056); prettier clean over the full
`origin/main...HEAD` set. No check ID claimed — 09 has no ID covering row 3, and the task section
binds verification at the improvement pass. **The L2 opportunity to record there:**
`dev/verify/broker-refuse-l2.sh` already induces exactly this condition, so an assertion on
`status.broker.journalReachable` could ride it without new setup.

---

## Milestone audit 2026-07-31 — the ladder finished, the ratchet did not

`harness-milestone` was invoked for Phase 9 and **stopped at §1**. Its four conditions were not all
proven, and §1's instruction when any is unproven is _"stop and return to `harness-run`"_. No gate
was run: §1 gates §2, and no amount of cluster time can change a fact established by reading the
scripts. What follows is that reading.

### The arithmetic first — the ladder really is done

The ledger's Phase 9 row carried a stale leaf count (`56 of 60`) and said so. Reconciled by parsing
every `P9-T…` ID in this file's task sections and reducing each to its parent, so that `T7c-2a`
rolls up to `T7c-2` and not to `T7c`:

|                                  |                                                                                        |
| -------------------------------- | -------------------------------------------------------------------------------------- |
| Leaf units in the Phase 9 ladder | **72**                                                                                 |
| Moved out of the phase           | **2** — `T7c-2b` → Phase 10 (the `/replay` deferral, human-owned), `T8b-3b` → `P10-T3` |
| In-phase leaves                  | **70**                                                                                 |
| Done                             | **70**                                                                                 |

So the _task ladder_ is complete. That is what made the stale count worth reconciling, and it is
also why the next paragraph is the finding rather than a footnote: a finished ladder is exactly the
condition under which a phase looks closeable.

### The finding — 14 of the 55 required checks are asserted by nothing

Every check ID in the "Acceptance → check binding" table above (Accept (a)–(e), the four ratchet-only
rows, and the carried V-CMP-006) — 55 unique IDs — was audited against `verification/results.csv`
and against every file in the tree that names it. Three populations came out:

> **Superseded, and kept as the record of how the numbers moved.** This audit's denominator was the
> hand-written table, and both the table and the derivation that reads it were wrong. `T11a` put the
> figure at 38/17, `T11a-2` at 28/12, `T11a-3` at 34/19 against a required set of 98, and `T11c′` at
> **22 not green of 82, 11 BLOCKING-ALWAYS**. The last is the live one; the sections at the end of
> this file carry the working for each move.

|                          | Count  | Meaning                                                                                                                                                            |
| ------------------------ | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Green**                | 22     | a `pass` row with an `evidence_ref` (09 §9.4)                                                                                                                      |
| **Asserted, unrecorded** | 19     | a test, script or policy in the tree names the ID; a gate run produces the row. Includes V-GAT-001 (only a `correction` row) and V-REV-007 (only a `decision` row) |
| **Asserted by nothing**  | **14** | no `dev/` script, no Go test, no policy, no results row. Nothing in this tree can produce a verdict for these IDs                                                  |

The fourteen, with the seven **BLOCKING-ALWAYS** ones in bold (09 §5: V-BRK, V-REV and V-ISO are
BLOCKING-ALWAYS suites, and 09 §9.6 forbids deferring any of their members):

| Check         | L      | 09 §6 text                                                                                                            |
| ------------- | ------ | --------------------------------------------------------------------------------------------------------------------- |
| **V-BRK-001** | L2     | From inside the agent container, a direct API write with the pod token fails ¬                                        |
| **V-BRK-004** | L2     | A write with the `kube-agents/action-id` annotation stripped is rejected at admission ¬                               |
| **V-BRK-016** | L2     | Post-execution journal failure — write lands, record cannot be completed: roll back, `RolledBack`, auto-pause, page ¬ |
| **V-REV-009** | L2     | A destructive undo is itself gated — undoing a `create` whose plan deletes a bound PVC parks ¬                        |
| **V-ISO-001** | L2     | CH1 controller down — agents **and brokers** keep executing; no new reconciles                                        |
| **V-ISO-002** | L2     | CH2 controller up after loss — relaunches **both** workloads, rebinds **both** SAs                                    |
| **V-ISO-006** | L2     | CH6 journal down — broker refuses to execute ¬                                                                        |
| V-RUN-001     | L2     | Exactly two workloads per `Agent` CR, both owner-referenced; no third, no minted SA ¬                                 |
| V-RUN-002     | L2     | Correct identity on each; neither settable to the other's value ¬                                                     |
| V-RUN-004     | L2     | Labels `tier`/`scope`/`parent`/`role` on Deployments, pods and Services, and selectable                               |
| V-RUN-005     | L2     | Startup ordering safe both directions; broker-first and agent-first both converge                                     |
| V-RUN-006     | L2     | Agent with no broker fails closed into observe-and-report; no direct-write fallback ¬                                 |
| V-RUN-009     | L2     | Deleting the CR removes both workloads and leaves both SAs intact                                                     |
| V-RUN-014     | L0, L2 | One Socket Mode connection, fleet-wide; no agent pod holds an app token ¬                                             |

Two of these need the record straightened rather than merely built:

- **V-ISO-001/002 are cited in `pair_netpol.go` and `pair_netpol_test.go`, and both citations are
  disclaimers.** `pair_netpol_test.go:35` says _"V-ISO-001/002 ask whether a packet is DROPPED, which
  is L2 and belongs to P9-T9"_; `pair_netpol.go:68` records the same. A grep for the ID finds a file;
  the file says it is not the assertion. That is the correct thing for those comments to say, and it
  is why the audit had to read every hit rather than count them.
- **`dev/verify/chaos-suite.sh` contains the word "broker" zero times.** Its C1 asserts that a
  stand-in pod stays Ready, that the agent Deployment is not recreated, and that reconcile resumes.
  Every one of those is about the agent. V-ISO-001's property is _"agents **and brokers**"_ and
  V-ISO-002's is _"**both** workloads … **both** SAs"_. The suite predates the pair; it was never
  wrong, it was written when there was one workload.

### This is planning defect 4, arriving exactly as it predicted

Defect 4, written at PLAN on 2026-07-27, says the ratchet contains _"seventeen ratchet checks unrun"_
that no Accept bullet names, and declares the resolution: **"`verify-phase9.sh` runs the ratchet, not
the Accept list."** The acceptance table above was amended with four ratchet-only rows, which is half
the resolution. The other half was never built. `dev/verify/verify-phase9.sh` has sections A–I:
A is the L0 chain, B–F are Accept (a)–(e), G is the phase's own unfinished work, H is _prior_
regression via `verify-phase8.sh`, and I prints deferrals. **There is no section for V-ISO at all**,
and the script names 18 check IDs in total against a ratchet of 55.

This is [[LSN-019]]'s shape once more: prose on the artifact is not a mechanization. The resolution
was written into a table a human reads and not into a script a machine runs, so the phase's own
prediction of its own gap sat in the file the whole time and changed nothing. The count matching
(17 predicted, 14 unasserted plus the 3 the disclaimers cover) is not a coincidence — it is the same
list.

### What the milestone did NOT do, deliberately

- **No gate run.** `harness-milestone` §1 gates §2. Running the full gate would have consumed roughly
  an hour of scratch-cluster time to rediscover, from a red section, what reading the scripts already
  established — and it could not have discovered the 14, because a gate that never names an ID cannot
  go red for it.
- **No section-H back-fill, no ratchet extension, no `L2_CHAIN_FLOOR` move.** Same reason the
  2026-07-30 merge did not: they would be unearned.
- **No deferral rows.** Seven of the fourteen are BLOCKING-ALWAYS and 09 §9.6 forbids deferring them.
  The other seven are ordinary scheduled work, not blocked on anything external.

Precondition P1 was satisfied on the way in and is not wasted: all seven first-party images were
rebuilt from `895aaf3` through Cloud Build and deployed **by digest** to `gke-scratch-kube-agents-dev`
(`dev/cluster/reload-images.sh all`, exit 0). The cluster is at HEAD for whichever unit runs next.

### The ladder this opens — P9-T11

Four units, ordered so the gap becomes **detected** before it is closed. `T11a` is a check-only unit
and is red by construction on today's tree; that is the point, and it is the same "detected rather
than remembered" shape section G already uses. Guardrail 9 is satisfied structurally: `T11a` ships no
implementation, and `T11b`–`T11d` ship no change to `T11a`'s arm.

| Task                           | What to build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Spec                             | Files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Check IDs                                                                                          | Weight  |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- | ------- |
| **P9-T11a** ✅                 | The ratchet arm planning defect 4 declared and never built. A new section in `verify-phase9.sh` that derives the Phase 9 ratchet from 09 §10 + the acceptance table and **fails on any member with no green `results.csv` row** — so the gate reports the gap instead of omitting it. Prints the three populations above. Red today, and its redness IS the worklist.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | 09 §10; planning defect 4        | `dev/verify/verify-phase9.sh` · possibly `dev/tests/` for the L0 half                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | V-MET-013, V-MET-014 (the arm is itself a check about checks)                                      | small   |
| **P9-T11a-2** ✅               | **The ratchet arm under-counts green.** `parse_results` keys on the raw `check_id` cell, so a row naming `V-ISO-001, V-ISO-002` is filed under that literal string and matches neither ID. 36 of the 160 rows group IDs that way — it is the file's convention for a suite proving several rows at once, not an anomaly. Split the cell on the `V-XXX-nnn` pattern already in the module as `CHECK_ID`. Add a `--negative-control` case for a grouped row, since the current control only ever synthesises single-ID rows and is therefore blind to this.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 09 §9.4                          | `dev/tests/phase-ratchet-is-asserted.py`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | V-MET-013, V-MET-014                                                                               | small   |
| ~~**P9-T11b**~~                | **Split on 2026-07-31 into T11b-1 and T11b-2** (`harness-run` §2 Sizing — the row was weighted `large` and carries two independent suites, each needing its own destructive L2 run against the scratch cluster).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 05 §8; 03 §4.1                   | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | —                                                                                                  | large   |
| **P9-T11b-1** ✅               | V-ISO-001/002 — chaos for the **pair**. C1 and C2 asserted the agent workload only; extend both to the broker Deployment, both ServiceAccounts and both rebinds. Put `chaos-suite.sh` on `dev/L2-CHAIN.txt`, which it had never been on.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 05 §8                            | `dev/verify/chaos-suite.sh` · `dev/L2-CHAIN.txt`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | **V-ISO-001**, **V-ISO-002**                                                                       | medium  |
| **P9-T11a-3** ✅               | **The ratchet arm ignores 09 §6's Phase column, and reads one row of 09 §10.** A bare suite name expanded to every member, so a shadow phase was asked for V-BRK-016 (the write lands), V-BRK-003 (real audit writes) and V-RUN-014 (§6: phase **15**); and §10's _"once a suite enters the ratchet it never leaves"_ was unimplemented, so V-CTN, V-CTR, V-CMP and V-MET — all in since phase 8 — were not required at all. Expand each suite against the catalog's due date, accumulate every row ≤ N, keep IDs §10 names outright unfiltered, and PRINT what the filter removed. 21 out, 31 in: **70 → 80**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 09 §6 preamble; 09 §10           | `dev/tests/phase-ratchet-is-asserted.py` · `dev/test_invariants_gate.py`                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | V-MET-013, V-MET-014                                                                               | small   |
| **P9-T11b-2** ✅               | V-ISO-006 — CH6, journal down → broker refuses to execute. Arm B was bound to the ID it already proved, and **arm C was added**: CH6's last clause — restoring the journal restores service _without a broker restart_ — separates a broker that refuses from one that bricks, and arm B passes either way. Cross-reference added in `chaos-suite.sh` instead of a second, thinner CH6.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 03 §4.1; 06 §4.4 row 3           | `dev/verify/broker-refuse-l2.sh` · `dev/verify/chaos-suite.sh`                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | **V-ISO-006**                                                                                      | small   |
| **P9-T11c′** ✅                | **This file's own acceptance table demanded 16 IDs 09 §6 dates after phase 9** — V-BRK-001/003/004/016, V-GAT-019/021/022, V-REV-002/005/006/007/008/009, V-RUN-006/013/014. The required set is the UNION of §10 and this table, so the table kept every one of them required. Retargeted to the phase §6 names, recorded in their own `##` section so the record and the requirement do not share a parse. Required **98 → 82**, not green **34 → 22**, BLOCKING-ALWAYS **19 → 11**. Split out of `T11a-3` under Guardrail 9: that unit moved the measurement, this one moves the verdict.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 09 §6; 09 §10                    | `docs/build/phase-9.md`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | V-MET-013                                                                                          | small   |
| **P9-T11c″** ✅                | **The control was coupled to the document it audits, in three places, and read 20/20 throughout.** `stage()`'s `name_in_table` PREPENDED instead of replacing; three victim pools filtered on _"not named by the table"_; and the under-naming case staged the live document. Completing the table took the control from 20/20 to **unstageable**. Repaired: one `victim_pools()` definition site for staging and audit, a stageability guard that re-runs the pools against a complete and a retargeted table on every ordinary run, and `_control_against()` in the unit suite so both future trees are committed cases rather than a `/tmp` probe ([[LSN-053]]). Check-only, no artifact change — and **landed before `T11c′`**, which is the unit's own finding. Followed by **`P9-T11c″-b`**: the repair had the same coupling in it, one level up — both future-tree fixtures and the guard derived their hypothetical table from the live one, so `T11c′` collapsed it. Fixed to derive from 09 alone, detector lifted out and tested; sweep 6/6 → **9/9**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | [[LSN-053]]                      | `dev/tests/phase-ratchet-is-asserted.py`, `dev/test_invariants_gate.py`                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | V-MET-013, V-MET-014                                                                               | small   |
| **P9-T11c‴** ✅                | **The acceptance table named 39 of the 82 obligations the phase is closed against.** The other 43 (V-BRK-018, V-BRK-022…032, V-CTN-001/012/015/016/017/020/037, V-CTR-001/002/003/006/007/014/015/016/017/018/020, V-MET-001…009/013/014, V-REV-010/011) reached the required set only through 09 §10's suite names, so the phase file both was and was not the record of what it owed. Added as eight themed _(ratchet only)_ rows, each carrying its level and target from 09 §6. **The required set did not move — 82 before, 82 after** — which is the whole point: nothing was added to the gate, the gate was written down. Property 4 silent; the union is now the table plus nothing.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 09 §10                           | `docs/build/phase-9.md`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | V-MET-013                                                                                          | small   |
| **P9-T11f** ✅                 | **17 of the 251 check IDs 09 mentions have no 09 §6 catalog row** — 14 V-CMP (001–008, 010, 011, 020–023, prose bullets in §5) and **V-MET-010/011/012** (§14). §6 calls itself _"the authoritative index"_; a check absent from it is a check no suite-name expansion can reach, and both suites are in the phase-8 ratchet row. Three are BLOCKING-ALWAYS. All three are implemented — the hole is in the index, not the work — but a gate that cannot see a BLOCKING-ALWAYS check cannot fail on it. Catalogued: a new **§6.15** for the fourteen V-CMP, three rows appended to the §8 V-MET table, and every §5/§14 prose bullet de-bolded so each ID keeps one definition site. Required **82 → 91**, not green **22 → 26**, BLOCKING-ALWAYS not green **11 → 13**; the unit's own V-MET-010 run then closed one, ending at **25 / 12**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 09 §5, §6, §14                   | `docs/design/09-verification-and-validation.md` · `docs/build/phase-9.md`                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | V-MET-010, V-MET-013                                                                               | medium  |
| ~~**P9-T11g**~~                | **Split at ORIENT on 2026-07-31 into `-1`…`-4`, because its premise was wrong.** The row promised _"eleven runs to be made, not rows to be written"_ and two builds. Auditing all thirteen against the tree found **one run and twelve builds**: only V-MET-012 was implemented-and-unrecorded. It also found a **fourteenth** not-green BLOCKING-ALWAYS check, **V-MET-002**, which this file names nowhere and which reaches the required set only through `T11c‴`'s themed `V-MET-001…009` ratchet row. Full audit in § `P9-T11g-1` below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 09 §9.4; 09 §10                  | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `T11g-1`…`T11g-4`                                                                                | —       |
| **P9-T11g-1** ✅               | **The audit, the re-split, and the one artifact that closed two IDs.** Established for each of the thirteen whether its property is asserted anywhere: V-MET-012 is `dev/tests/spec-ids.py` and green on every run (recorded); everything else is unbuilt. Built `dev/tests/crd-has-no-authority-fields.py`, which closes **V-CTR-003 and V-CMP-011** together because 06 §10 states them in one sentence — five properties (no prohibited name at any depth under `spec`, no `x-kubernetes-preserve-unknown-fields` at any depth, a non-vacuity floor, **the Go source's JSON tags** since no PR check regenerates the CRD, and the L2 V-9 pruning arm still existing), `--negative-control` 6/6. Required 91 unchanged; not green **25 → 22**, BLOCKING-ALWAYS **12 → 11**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 06 §10; 09 §6.5, §6.15           | `dev/tests/crd-has-no-authority-fields.py` · `dev/L0-CHAIN.txt`                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | V-CTR-003, V-CMP-011, V-MET-012                                                                    | medium  |
| ~~**P9-T11g-2**~~              | **Split at IMPLEMENT on 2026-07-31 into `-2a` and `-2b`, because the row's "one tool over `verification/traceability.yaml`" pointed at a file that answers a different question.** ~~Neither `verification/traceability.yaml` nor `verification/coverage.yaml` is in the tree~~ — **corrected 2026-07-31, same day, by the next unit's ORIENT: `verification/traceability.yaml` HAS existed since `P8-T10` (`ead358e`), 71 KB, 177 entries.** The split's conclusion survives the correction and its stated reason did not, which is the part worth keeping: `traceability.yaml` maps the **177 Verification bullets** of 01–08 to check IDs, and that is V-MET-011's artifact, already green. 09 §8 asks for something an order of magnitude larger — `R-<doc>.<section>-<n>` over **every normative statement** (must / never / always / is rejected / is a defect / may not, plus every mandated-behaviour table row), which §8.1 counts at ~538. That ID space has never been minted: repo-wide, the only two occurrences of `R-<doc>.<section>-<n>` are the two sentences in §8 that _define_ it. `verification/coverage.yaml` is genuinely absent. V-MET-002/008/009 read a requirement→check mapping; V-MET-001 reads a check→implementation one; only the second was a session's work.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 09 §8, §8.1                      | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `T11g-2a`, `T11g-2b`                                                                             | —       |
| **P9-T11g-2a** ✅              | **V-MET-001 — and the artifact it needed did not exist.** 09 §8 states it as two directions between two sets; nothing in the tree held the second one. The nearest thing was the ratchet's `hint: named by …` column, a `git grep` under a footer disclaiming it — **and that disclaimer was earned again this phase**: `T11g` was scheduled on the hint column promising eleven runs, and nine of the eleven were "named by" a parser fixture, by `binding.md` and the skills that _require_ the check, or by VAP corpora nothing runs. Built **`verification/implementations.yaml`** (a curated `check-id → {runs, asserts_in}` registry, 81 entries) and **`dev/tests/check-ids-have-implementations.py`**, seven properties: registry keys are §6 IDs; every `asserts_in` exists **and names its own check ID**; every `runs` appears verbatim on a chain (or is one of the two entry points no chain line carries) **and reaches the file it claims**; forward-phase-scoped over required∩green; every check-ID token in the test corpus is §6-defined; and the `unimplemented:` count is pinned at 1. `--negative-control` **8/8**. Three findings, below. Not green **22 → 21**, BLOCKING-ALWAYS **11 → 10**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | 09 §8; 09 §9.4                   | `verification/implementations.yaml` · `dev/tests/check-ids-have-implementations.py` · `dev/L0-CHAIN.txt` · 6 test files given a check-ID declaration                                                                                                                                                                                                                                                                                                                                                                                       | V-MET-001                                                                                          | medium  |
| ~~**P9-T11g-2b**~~             | **Split at SELECT on 2026-07-31, before any code, because the row was sized `large` and the sizing was real.** 09 §8's population is ~538 by the §8.1 shape; a mechanical count over 01–08 puts it near 900. Enumerating that, curating a requirement→check mapping over it, and building three BLOCKING-ALWAYS checks is not one session, and `harness-run` §2 forbids carrying an oversized unit forward. Split into the **denominator** (`-2b-i`) and the **numerator and its gates** (`-2b-ii`), with `-2b-0` falling out of `-2b-i` under Guardrail 9. The seam is load-bearing rather than convenient: V-MET-002 and V-MET-008 are both statements about a ratio, and neither can be built — or falsified — before the thing being divided by exists.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 09 §8, §8.1                      | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `T11g-2b-0`, `T11g-2b-i`, `T11g-2b-ii`                                                           | —       |
| **P9-T11g-2b-0** ✅            | **The spec-text exemption follows the content into a generated mirror.** Two L0 lints scan every tracked file for bytes that must not appear in source — the retired `ALLOW_ALL_USERS` hatch (V-CTR-002/V-CTR-014) and any `kubeagents.` domain the operator does not serve (LSN-032) — and both already exempt `docs/design/`, because a spec has to describe what was removed. `verification/requirements.yaml` is a verbatim mirror of those sentences under a scanned suffix, so both fired: 07 §2's acceptance text names `*_ALLOW_ALL_USERS` while describing its deletion, and 06 §8's observability table names eleven OTel attribute keys. **Split out under Guardrail 9** — a check may not change in the same unit as the work whose failure motivated it. Both entries are an **exact path, never a `verification/` prefix**, following the precedent set on 2026-07-26 when V-CTR-014's evidence rows moved into `results.csv`. For the group lint this is the narrower of the two fixes available: widening `NON_GROUP_ATTRIBUTES` would have exempted those eleven spellings in Go and YAML source too, and that set is closed precisely because an attribute-key typo is the same silent defect as a group typo. Over-breadth **probed, not assumed** — a sibling `verification/probe-sibling.yaml` carrying both an emission and `kubeagents.wrong.io` is still caught by both.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 09 §8; LSN-032                   | `dev/tests/closed-allowlist.py` · `dev/tests/api-group-single-sourced.py`                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | V-CTR-002, V-CTR-014 (regression)                                                                  | small   |
| **P9-T11g-2b-i** ✅            | **V-MET-009 — the denominator 09 §8 asks for did not exist.** _"Every normative requirement in 01–08 maps to at least one check ID"_ had no population behind it, so §8.1's _"roughly 45% uncovered"_ was a percentage with no list — the exact shape V-MET-009 forbids. Built **`verification/requirements.yaml`** (853 requirements, `R-<doc>.<section>-<n>`, generated), **`verification/coverage.yaml`** (the uncovered list published **by ID**), and **`dev/tests/requirements-are-enumerated.py`**, which is both enumerator and lint over its own output. Six properties: the enumeration is current against the specs **by text, not count**; IDs are well-formed and contiguous 1..n; coverage agrees with the enumeration; the uncovered list is **published, not counted**; it is complete and resolves; and the recorded §8.1 baseline matches the spec cell for cell. `--negative-control` **8/8**. Not green **21 → 20**, BLOCKING-ALWAYS **10 → 9**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | 09 §8, §8.1, §9.4                | new `verification/requirements.yaml` · new `verification/coverage.yaml` · new `dev/tests/requirements-are-enumerated.py` · `dev/L0-CHAIN.txt` · `verification/implementations.yaml` · `verification/results.csv`                                                                                                                                                                                                                                                                                                                           | V-MET-009                                                                                          | medium  |
| ~~**P9-T11g-2b-ii**~~          | **Split at SELECT on 2026-07-31, before any code, because the numerator is a curation job and the machinery is not.** The row asked for the curated `checks:` mapping **and** both gates. Measuring first: deriving ownership from 09 §6's `Source` cells puts **357 of the 853** requirements under V-CTN/V-BRK/V-REV/V-ISO/V-ADV — 267 table rows and 90 prose statements across 32 spec sections — and **V-MET-002 demands every one of them map to a real check**. Section-citation cannot be that mapping: a requirement is _owned_ precisely because some load-bearing check cites its section, so reusing the citation as coverage makes V-MET-002 green by construction — the exact false green 09 §8.1 distinguishes _fully covered_ from _partial_ to avoid. So the mapping has to be argued statement by statement, and that is not one session with margin on top of two BLOCKING-ALWAYS lints. **The seam is ownership-and-arrival versus the draw-down:** V-MET-008 needs ownership (it governs the requirements that are _not_ load-bearing) and needs no curation at all, because §8.1 scopes it to _"a **new** normative statement arrives with a check or a named deferral"_. V-MET-002 needs the curation and nothing else.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 09 §8, §8.1                      | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | —                                                                                                  | —       |
| **P9-T11g-2b-ii-1** ✅         | **Ownership, derived and published — and the ratchet that needs no curation.** Derive requirement→suite ownership from 09 §6's `Source` cells (`03 §11`, `03 §3.3, §4.3` — a bare `§` inherits the last document named in the cell), plus **§6.4's prose case**: the V-ISO table has no `Source` column at all and its section says _"CH1–CH9 as defined in 05 §8"_, so a derivation that reads only cells silently gives a BLOCKING-ALWAYS suite zero owned requirements. **A load-bearing suite that derives to zero owned sections is a hard failure**, not an empty set — that is the mechanized form of the resume note's warning that whoever declares ownership can make V-MET-002 green by declaring everything unowned. Then build **V-MET-008**: §8.1's arrival clause over a **digest baseline** — a committed set of content digests of the normative statements as they stand today, so a statement added later is detected by its text rather than by a positional ID that every insertion above it shifts — plus the fall-protection clause over `totals` and a `deferrals:` register in the shape V-MET-006 already asks for. Publishes the per-suite uncovered list **by ID**, which is V-MET-002's measurement without V-MET-002's gate.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 09 §8, §8.1, §6.4                | `verification/coverage.yaml` · a coverage baseline file · `dev/tests/` · `dev/L0-CHAIN.txt`                                                                                                                                                                                                                                                                                                                                                                                                                                                | V-MET-008                                                                                          | medium  |
| ~~**P9-T11g-2b-ii-2**~~        | **Split at SELECT on 2026-07-31, before any code, on a measurement and an ordering constraint.** The **measurement**: the 357 are not spread evenly — **227 of them are document 06 alone** (§1.1 19, §1.2 21, §2.2.1 1, §2.3 7, §4.1 39, §4.2 52, §4.3 43, §4.3.1 8, §4.4 18, §7 7, §9 12), against **130** across the other seven documents' 21 sections. Every mapping decision costs one read of the statement and one read of the candidate suite's assertion text, and the candidate sets are thin where the sections are fattest — 06 §4.2 is 52 requirements against **two** candidates (V-BRK-022, V-BRK-024), 03 §9 is 11 against one. That is not one session with margin. The **ordering constraint** is the sharper half: `check-ids-have-implementations.py` requires a check's `runs` command to appear verbatim on a chain, and the chain must be green — so **V-MET-002 cannot be added to `dev/L0-CHAIN.txt` until the worklist is already empty.** The gate is not the same unit as the draw-down; it is downstream of it. Split by document, then the gate.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 09 §8, §8.1                      | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `T11g-2b-ii-2a`…`-2c`                                                                            | —       |
| **P9-T11g-2b-ii-2a** ✅        | **The draw-down mechanism, and document 06's 227.** The mapping is curated **into `verification/requirements.yaml`'s own `checks:` lists** — no second file, on `verification/traceability.yaml`'s precedent (curated, one long header carrying the grouped arguments, no per-entry rationale), because a separate decision table would be a second definition site for the same fact and the lint would then have to keep two artifacts in step. `--emit` already merges rather than overwrites, so the curation survives regeneration; `coverage.yaml` and `coverage-ratchet.yaml` are re-emitted so the floor rises and the worklist shrinks. Then apply it to **06** — the broker contract, whose owning suite (V-BRK) is the one this phase built. **Two rules inherited from the split.** (1) **Section-citation is not coverage** — a requirement is _owned_ precisely because some load-bearing check cites its section, so reusing the citation as the mapping makes V-MET-002 green by construction; the grouped cases (§4.1's envelope field rows, §4.2's rule fields, §4.3's `ActionRecord` fields) are grouped decisions with one written argument each, not automatic ones. (2) **A requirement with no honest check is left unmapped** — the gap is the finding, and it is published by ID. Claimed check is **V-MET-008 as a regression**: the floor may rise, and the ratchet must still hold across the re-emit. **✅ DONE 2026-07-31 — 222 of 06's 227 mapped, worklist 357 → 135, floor 0 → 222.** Five requirements left unmapped and published by ID rather than closed with the nearest-looking row: `R-06.2.3-6` (nothing asserts the _absence_ of a developer-team actor GSA), `R-06.4.2-17` (no check asserts `ChangePolicy` dialect admission), and `R-06.4.2-30`/`-44`/`-45` — one hole, no catalog check reaching secret-material handling inside the code floor; closing it is a **catalog** change. The re-emit kept all 222, which is the merge property this row claimed V-MET-008 for. **And it caught a broken control:** moving `coverage.yaml`'s `covered` off `0` turned V-MET-009's mutation into a no-op that reported `MISS` where the truth was `BROKEN`; repaired both ways and **LSN-063** opened for the nineteen other control loops with the same exposure.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 09 §8, §8.1                      | `verification/requirements.yaml` · `verification/coverage-ratchet.yaml` · `verification/coverage.yaml` · `dev/tests/`                                                                                                                                                                                                                                                                                                                                                                                                                      | V-MET-008 (regression)                                                                             | large   |
| **P9-T11g-2b-ii-2b**           | **The remaining 130, across seven documents and 21 sections.** 03 (55: §4.3 20, §9 11, §6 6, §8 6, §3.2 3, §3.1/§3.3/§4.1/§4.2 2 each, §1 1), 02 (27: §7 18, §6 8, §4 1), 04 (17, all §5.1), 05 (17: §1.2 9, §7 6, §1.3 2), 08 (7: §2.5 6, §2.3 1), 07 (5, all §5), 01 (2, §3). Same two rules, same mechanism — this row exists because the reading is per document and the documents are independent, not because the work differs. ✅ **DONE 2026-07-31.** 89 mapped here (118 with the 29 for 01 §3 and 02 §§4/6/7 done in the same session), coverage 222 → 340, floor 222 → 340, worklist 135 → **17**. Twelve further gaps published by ID rather than closed with the nearest-looking row: `R-03.4.3-8` (ancestor `Agent` CR), `R-03.4.3-9` (live tier template), `R-04.5.1-9`…`-16` (04 §5.1's **settle-window** table — V-PRO-013 exercises the _predicate_ table beside it), `R-05.1.2-2` (snapshot `Secret` digests), `R-07.5-4` (the _authority-never-precedes-machinery_ **ordering** gate). Each is a catalog change. Also repaired V-MET-008's floor mutation, which had expired against the literal it was keyed to — [[LSN-063]] recurring one unit after it was opened.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 09 §8, §8.1                      | `verification/requirements.yaml` · `verification/coverage-ratchet.yaml` · `verification/coverage.yaml`                                                                                                                                                                                                                                                                                                                                                                                                                                     | V-MET-008 (regression)                                                                             | medium  |
| ~~**P9-T11g-2b-ii-2c**~~       | **V-MET-002 itself.** Every load-bearing-owned requirement maps to ≥1 check; an unmapped one fails the build. Lands on `dev/L0-CHAIN.txt` only once `-2a` and `-2b` have emptied the worklist, per the ordering constraint above. If the worklist is **not** empty when this row is reached, that is the honest outcome and V-MET-002 stays red — 09 §8.1 dates the load-bearing draw-down to _"before Phase 10 grants the first write credential"_, and buying a green with a citation would be the false green that dating exists to prevent. A red BLOCKING-ALWAYS check at the milestone is a halt, not a deferral (09 §9.6). **Split at SELECT on 2026-07-31, before any code, on Guardrail 9.** The worklist was _not_ empty when this row was reached — sixteen obligations remain, and every one of them is unmapped because **no catalog row asserts the property**, not because a check is red (coverage ≠ greenness: V-MET-002 asks only that a requirement _maps to_ ≥1 catalog check). Closing them is therefore a **09 §6 catalog** change, which is exactly the artifact V-MET-002 measures — so writing the check and growing the thing it measures in one unit puts the check's author and its subject in the same diff, and the cheapest route to green becomes "measure less". Build the check first, red and honest, publishing all sixteen by ID; grow the catalog second. `-2c-i` carries the [[LSN-053]] obligation: it is green on the synthesised future tree as a committed `--negative-control` row.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 09 §8, §8.1, §9.6                | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `T11g-2b-ii-2c-i`, `-2c-ii`                                                                      | —       |
| **P9-T11g-2b-ii-2c-i** ✅      | **The check, red and honest.** `dev/tests/load-bearing-coverage-is-full.py` — three properties: **P1** non-vacuity (the load-bearing partition yielded ≥300 owned obligations, the enumeration ≥700 statements, no load-bearing suite owns nothing, the derivation resolved); **P2** every owned obligation maps to ≥1 check, one finding **per** unmapped obligation, by ID and in the statement's own words, never a count (09 §8.1's pairing with V-MET-009); **P3** every claimed ID resolves to a real 09 §6 catalog row. **Live arm is deliberately off `dev/L0-CHAIN.txt`**, on the `phase-ratchet-is-asserted.py` precedent recorded there: a required PR check that is red for a whole phase reddens every unrelated commit ([[LSN-055]]). It runs in `dev/verify/verify-phase9.sh` **section K**, and `invariants-gate.py::check_phase_gate_publishes_the_coverage_remainder` is what stops that section being deleted — two properties (invoked at all; its red reaches `bad` within twelve lines), seven control arms, grandfathering phases 2–8 by explicit list rather than by floor. The **`--negative-control` arm is on-chain** and runs against the **synthesised future tree** — `_future_tree()` fills each unmapped obligation's `checks:` with its first owner, asserts that base is GREEN before mutating (`BROKEN` if not), resolves its victim dynamically as the first owned obligation in document order, and scores **8/8** with the [[LSN-063]] no-op guard. **No `implementations.yaml` row and no `results.csv` row**: the registry's own rule is _"a check with no green row is absent, and that is correct — do not add a row for work you intend to do"_, and the absent pass row is precisely what `phase-ratchet-is-asserted.py` already reports. **✅ DONE 2026-07-31.** Live arm rc=1 publishing all sixteen; control 8/8; 32/32 invariant checks; 422 dev tests. **One correction landed with it:** `R-06.4.2-30` (`secret-material-egress`) was a curation error, not a stricter reading — it is a row of the same code-floor rule table as `secret-write` and `blast-radius-cap`, `RuleSecretMaterialEgress` is a member of `classify.AllFloorRuleIDs`, and the corpus carries nine cases for it, so V-MET-005 and V-GAT-001 reach it by exactly the argument that maps every sibling. Coverage 340 → **341**, worklist 17 → **16**. **And one finding for the improvement pass:** `verify-phase9.sh` section A's L0 line floor read `43` against a 56-line chain — thirteen lines of slack, the exact failure the sentence beside it describes. Raised to 57 and a derived-floor lint queued ([[LSN-019]]).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | 09 §8, §8.1, §9.6                | `dev/tests/load-bearing-coverage-is-full.py` · `dev/L0-CHAIN.txt` · `dev/verify/verify-phase9.sh` · `dev/tests/invariants-gate.py` · `dev/test_invariants_gate.py` · `verification/requirements.yaml` · `verification/coverage*.yaml`                                                                                                                                                                                                                                                                                                      | V-MET-008 (regression), V-MET-009 (regression)                                                     | medium  |
| ~~**P9-T11g-2b-ii-2c-ii**~~    | **The sixteen, and the green.** Grow the 09 §6 catalog with the rows that close them — each a real, provable property with a suite, a Source, a level and a phase, never a row written to be cited. **Split three ways at SELECT on 2026-07-31, on harness-run §2 sizing, before any code.** Sixteen obligations across seven unrelated properties is not one unit: each closure is _read the requirement, find or build the machinery, argue the row_, and the arguments do not share a shape — a doc-drift lint over two tables, a catalog row pointed at a gate that already exists, and rows whose machinery has to be found first. Carrying them together would also hold V-MET-002 red until the last one lands, which is the whole worklist hostage to its hardest member. Split by **what the closure costs**, not by document: `-a` is one lint closing eight rows, `-b` is the rows whose machinery is already in the tree, `-c` is the remainder plus the green.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 09 §6, §8, §8.1                  | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `-2c-ii-a`, `-2c-ii-b`, `-2c-ii-c`                                                               | —       |
| **P9-T11g-2b-ii-2c-ii-a** ✅   | **The settle windows — eight of the sixteen, one lint.** `R-04.5.1-9`…`-16` close together because they are one table. **V-REV-012** (09 §6.3, Source 04 §5.1, L0, phase 9): `dev/tests/settle-windows-match-the-spec.py` reads the durations out of 04 §5.1's **window** table and out of `k8s-operator/internal/broker/verify/predicate.go`'s `settleWindows` map and compares them cell for cell. Five properties: non-vacuity (exactly the eight labels parse, all three constants found, the ceiling sentence found, no spec label left unbound); every published row resolves to its code entries with the same duration, positionally where a row names N kinds; nothing in the map is unpublished; `MaxSettleWindow` equals the stated 30 minutes and no row exceeds it; `DefaultSettleWindow` equals the `Custom resource` row. **Why it is a check at all:** §5.1 holds **two** per-kind tables, V-PRO-013 owns the predicates, and the section says in as many words why it states numbers — _"'Bounded' on its own is unfalsifiable — any number satisfies it — so the windows are stated here rather than left to the implementation."_ A check that waits for a rollout to settle waits **some** window and cannot fail when a constant drifts. **✅ DONE 2026-07-31.** Live arm green, negative control **12/12 from both ends of the drift** — six mutations per artifact, because a doc-drift lint exercised on one side only is half a check. **Worklist 16 → 8, coverage 341 → 349, floor rebaselined, 04 covered 9 → 17.** Two sizing decisions are written into the check itself: **no floor on the code-entry count** (a broken parse already yields eleven findings naming eleven kinds, which is louder and better attributed than a count — and a floor at the map's real size would make the _falls through to the default_ branch unreachable by any single-entry deletion, which is protection no control can exercise), and the insert mutation is **anchored on `15s`** because both of §5.1's tables carry an `RBAC` row and the unanchored form edited the one this check does not read.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 09 §6.3, 04 §5.1                 | new `dev/tests/settle-windows-match-the-spec.py` · `docs/design/09-verification-and-validation.md` · `verification/requirements.yaml` · `verification/coverage.yaml` · `verification/coverage-ratchet.yaml` · `dev/L0-CHAIN.txt` · `verification/implementations.yaml` · `verification/results.csv`                                                                                                                                                                                                                                        | V-REV-012, V-MET-008/009/001 (regression)                                                          | medium  |
| ~~**P9-T11g-2b-ii-2c-ii-b**~~  | **The rows whose machinery is already in the tree.** Four closures, each a **catalog** change pointed at code that exists rather than a new implementation — surveyed before the split so the unit is row-writing and not discovery. `R-07.5-4` (_authority never precedes machinery_) → `invariants-gate.py::check_write_verbs_have_machinery`; it is the instructive one, unmapped **only** because no catalog ID claims it. `R-06.4.2-17` (a `fieldPaths` entry beginning with `/` is rejected at `ChangePolicy` admission) → `ValidateDottedPath` plus `changepolicy_webhook.go:138`. `R-05.1.2-2` (snapshots stripped of `managedFields` and of `Secret` `data`, with a per-key digest instead of material) → `internal/journal/snapshot.go`. `R-06.4.2-44`/`-45` (the live-Secret digest **comparison method**, and that digests are never journaled or logged) → `internal/broker/classify/secretegress.go` (`DigestSet`, `NewDigestSet`, `DigestCacheTTLSeconds`). Each row must state a property the named code actually asserts **under test**: a row pointed at code with no assertion behind it is V-MET-014's failure wearing a catalog ID, and V-MET-001 refuses it unless the `runs:` line is on a chain. **Split two ways at IMPLEMENT on 2026-07-31, on harness-run §2 sizing.** The pre-split survey said "four catalog rows"; the machinery survey done at IMPLEMENT said two of the four need a **new assertion** and three need a Go mutation sweep (the existing sweeps in this tree run 6–22 mutants each), which is a different unit. The split axis is **whether the closure authors a new property assertion**: `-b-1` adds only negative controls over assertions that already exist, `-b-2` must write one, because `R-06.4.2-45` is a three-clause conjunction whose middle clause — _digests are computed in-broker, held in memory, **never journaled and never logged**_ — has no assertion anywhere in the tree, and a catalog row pointed at code with no assertion behind it is V-MET-014's failure wearing a catalog ID.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 09 §6, §8                        | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | → `-2c-ii-b-1`, `-2c-ii-b-2`                                                                       | —       |
| **P9-T11g-2b-ii-2c-ii-b-1** ✅ | **The two that need only a control.** **V-CTN-038** (09 §6.1, Source 07 §5, L0, phase 9, ¬) closes `R-07.5-4`: no agent-identity `Role`/`ClusterRole` anywhere in the tree — `.yaml.template` included, because a verb that appears only after envsubst is still granted — carries a verb outside `get`/`list`/`watch` while the broker, risk classifier, `ActionRecord` journal or undo path is missing; `*` is a write verb; the finding names the file, the verb and **every** absent item; ordering, not prohibition, so Phase 10 satisfies the row rather than deleting it. **V-CTR-021** (09 §6.9, Source 06 §4.2, L1, phase 9, ¬) closes `R-06.4.2-17`: the two path dialects are never interchangeable at any of the three places one could be accepted for the other — admission (with the message 06 §4.2 specifies, against the offending **index**, exactly once), `ValidateChangeRule` for objects that never met the webhook, and the matcher, which matches **nothing** rather than helpfully normalising. **✅ DONE 2026-07-31.** Controls **9/9** and **7/7**. **Worklist 8 → 6, coverage 349 → 351.** Three findings are written into the artifacts rather than this row: (a) invariant 7's green came from `missing_machinery()` being empty, **not** from an absent write verb — eleven documents in this tree do grant one — so the branch that raises the finding had never executed and M7 shows it would have named only the first absent item; (b) `invariants-gate.py` carried a paragraph whose argument rested on nothing named V-CTR-021 existing, which issuing the ID expired, so it now dates the claim; (c) the assertion baseline was wound twice because the first winding happened mid-IMPLEMENT, before a test's name was final, and the deletion arm then reported a test gone that never reached a commit.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 09 §6.1, §6.9, 07 §5, 06 §4.2    | `docs/design/09-verification-and-validation.md` · `dev/tests/invariants-gate.py` · `dev/test_invariants_gate.py` · `dev/assertion-baseline.json` · new `verification/mutants/V-CTN-038.json` · new `verification/mutants/V-CTR-021.json` · `k8s-operator/internal/webhook/changepolicy_webhook_test.go` · `verification/requirements.yaml` · `verification/coverage.yaml` · `verification/coverage-ratchet.yaml` · `verification/implementations.yaml` · `dev/tests/requirements-are-enumerated.py` · `verification/results.csv`           | V-CTN-038, V-CTR-021, V-MET-001/003/008/009 (regression), V-MET-002 (red, expected)                | medium  |
| **P9-T11g-2b-ii-2c-ii-b-2** ✅ | **The two that need a new assertion.** **V-OBS-008** (09 §6.10, Source 05 §1.2, L1, phase 9, ¬) closes `R-05.1.2-2`: a journal snapshot is stripped of `managedFields` and of `Secret` `data`, and a `Secret`'s pre-state is recorded as a **per-key** digest rather than material — `internal/journal/snapshot.go`, already asserted by `TestSanitizeStripsManagedFields`, `TestSanitizeStripsLastAppliedConfiguration` and `TestSanitizeDigestsSecretDataPerKey`, so this one is a control plus a sweep. **V-GAT-024** (09 §6.5, Source 06 §4.2, L1, phase 9, ¬) closes `R-06.4.2-44` and `-45`, and is the reason for the split: `-44` (the comparison method is a match against live `Secret` values, `sha256(secretNamespace ‖ 0x1f ‖ value)`, never a heuristic) is asserted, and so is `-45`'s third clause (`reasons[]` names the source `Secret` and key, never the value — `TestSecretHitNeverCarriesTheValue`), but `-45`'s **middle** clause is asserted by nothing. Planned closure: a **closed-allowlist** Go test in the shape `dev/tests/closed-allowlist.py` uses — `*DigestSet`'s exported method set is exactly `{Lookup, Len}` and neither returns a digest, and `SecretHit`'s field set is exactly `{Namespace, Secret, Key, Where, Form}` — which makes a digest **unreachable** outside package `classify` and therefore impossible to journal or log, rather than asserting that no current call site does it. Non-test `DigestSet` users are already confined to `internal/broker/classify/` and `internal/broker/livestate/`. **✅ DONE 2026-07-31.** Sweeps **9/9** and **10/10**. **Worklist 6 → 3, coverage 351 → 354.** Two things came out differently from the plan and both are recorded in the artifacts rather than here. (a) V-OBS-008 was surveyed as "a control plus a sweep" and is not: the three existing tests read named fields back out of the sanitized map, so material that escaped into an annotation or a nested list satisfies all of them, and — the real gap — **nothing joined the two packages**. `journal.Sanitize` writes the `sha256:` marker and `undo.RedactedSecretKeys` refuses on it; each side stated the contract in a doc comment from its own end and neither was a test. A drift there does not fail, it writes the hex of each value's own digest into the live Secret and reports a completed undo. The new file is an external test package (`journal_test`) so it may import `undo`, which imports `journal`. (b) V-GAT-024 grew a third property beyond the planned closed allowlist: the **formula** itself was unasserted — `Len()` was never pinned, so the `0x1f` separator and the closed set of three forms could both drift silently — and the package's freedom from a logger, which is what makes "never logged" a property of the code rather than of today's call sites. **FINDING, queued for the improvement pass:** `url.QueryEscape(testSecretValue) == testSecretValue`, so the `url` arm of `TestSecretMaterialMatchesEncodedForms` has taken its `t.Skip` on every run since it was written and has never asserted anything; changing the constant ripples through sixteen tests including the connection-string limitation fixture, so it is not this unit's to fix — the form is covered from here on by V-GAT-024's own fixture. | 09 §6.5, §6.10, 05 §1.2, 06 §4.2 | `docs/design/09-verification-and-validation.md` · new `k8s-operator/internal/broker/classify/digest_containment_test.go` · new `k8s-operator/internal/journal/snapshot_redaction_test.go` · `dev/assertion-baseline.json` · new `verification/mutants/V-OBS-008.json` · new `verification/mutants/V-GAT-024.json` · `verification/requirements.yaml` · `verification/coverage.yaml` · `verification/coverage-ratchet.yaml` · `verification/implementations.yaml` · `dev/tests/requirements-are-enumerated.py` · `verification/results.csv` | V-OBS-008, V-GAT-024, V-MET-001/003/008/009 (regression), V-MET-002 (red, expected)                | medium  |
| ~~**P9-T11g-2b-ii-2c-ii-c**~~  | **The remainder, and the green.** The three with no machinery yet: `R-03.4.3-8` (no write to an `Agent` CR whose identity is an **ancestor** of the writer's — V-CTN-007 covers the writer's own CR, V-CTN-025 the brake field on a **child's**, and nothing walks `parentRef` upward), `R-03.4.3-9` (actor writes stay inside the **live** tier template — both template checks are the inlined-literal form, which is 03 §4.2's whole point), `R-06.2.3-6` (the **absence** of a developer-team actor GSA — every containment check asserts what a principal cannot do, never that a principal does not exist). Then, once the worklist is empty: turn **V-MET-002** green, **move its live arm onto `dev/L0-CHAIN.txt`**, add its `implementations.yaml` row, and **retire `check_phase_gate_publishes_the_coverage_remainder` in the same commit that moves the line** — so the two are never simultaneously absent and the arm is never uncovered.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 09 §6, §8, §8.1, §9.6            | `docs/design/09-verification-and-validation.md` · `verification/requirements.yaml` · `dev/L0-CHAIN.txt` · `verification/implementations.yaml` · `dev/tests/invariants-gate.py` · `dev/test_invariants_gate.py` · `dev/verify/verify-phase9.sh`                                                                                                                                                                                                                                                                                             | V-MET-002, V-MET-008 (regression)                                                                  | ✅ done |
| ~~**P9-T11g-3**~~ ✅ done      | **✅ done 2026-07-31.** Three L0 containment arms landed green; **V-CMP-020 is `deferred`**, committed **red on purpose** and deliberately off `dev/L0-CHAIN.txt` — blocker **07 P13-T5**, owner phase 13, promotion condition `python3 dev/tests/tier-skills-match-the-allocation.py` exits 0. The row's premise held for the two V-CTN arms and was wrong twice for V-CMP-020: 09 §6 dates it phase **8**, not 9, and 07 P13-T5's _"none of the seven workload skills"_ is stale — the tree already gives the Developer Team four. Also corrected here: the controller does **not** create ServiceAccounts; the marker is `serviceaccounts,verbs=get;list;watch`. Write-up in § `P9-T11g-3` below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | 02 §2.1; 03 §11; 08 §7           | new `dev/tests/` lints · `dev/L0-CHAIN.txt`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | V-CMP-020, V-CTN-004 (L0), V-CTN-017 (L0)                                                          | medium  |
| **P9-T11g-4**                  | **The L2 containment arms on `gke-scratch-kube-agents-dev`.** **V-CTN-001** (reader SA reads only within tier scope; `no` in any other namespace/cluster/project), **V-CTN-012** (a `Role`/`ClusterRole` exceeding its tier template is denied by `vap-agent-scope` — the corpora at `examples/gitops-repo/policy/tests/vap_actor_{positive,negatives}.yaml` exist and **nothing on either chain runs them**, which is the finding), **V-CTN-015** (a duplicate `(tier, scope)` `Agent` CR is rejected), **V-CTN-016** (developer-team `metadata.namespace` must equal `spec.scope.namespace`), plus the L2 halves of **V-CTN-004** and **V-CTN-017**. All BLOCKING-ALWAYS. New `dev/verify/` script + `dev/L2-CHAIN.txt` line; L3 arm of V-CTN-001 is a deferral, not a pass.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 03 §4.2, §11; 08 §7              | new `dev/verify/*-l2.sh` · `dev/L2-CHAIN.txt`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | V-CTN-001, V-CTN-004 (L2), V-CTN-012, V-CTN-015, V-CTN-016, V-CTN-017 (L2)                         | large   |
| ~~**P9-T11c**~~                | **Dissolved — `T11c′` landed.** All four of its check IDs carry 09 §6 Phase **10**: V-BRK-001 (pod-token direct write), V-BRK-004 (stripped `action-id` — its `vap-agent-scope`-class policy does not exist until P10-T1), V-BRK-016 (post-execution journal failure — the write lands, which phase 9 has no authority to do) and V-REV-009 (destructive-undo gate). None is a phase-9 obligation; each is a phase-10 one, and `T11c′` removed them from the required set on 2026-07-31. Do not build.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | 03 §4.1, §4.3, §6, §11           | —                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | all four → phase 10                                                                                | —       |
| **P9-T11d**                    | The workload pair at L2: two workloads owner-referenced and no third, non-interchangeable identities, the four labels selectable, both startup orders converging, agent-without-broker failing closed, CR deletion removing workloads and sparing SAs, and one fleet-wide Socket Mode connection.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 08 §2.4, §7; 05 §8, C15          | `dev/verify/` (new pair suite) · `dev/L2-CHAIN.txt`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | V-RUN-001, V-RUN-002, V-RUN-004, V-RUN-005, V-RUN-009 (V-RUN-006 → phase 10, V-RUN-014 → phase 15) | large   |

**Resume at `harness-run`, unit `P9-T11g-2`** (this section was written at `T11a` and said
`P9-T11a`; `T11a`, `T11a-2`, `T11a-3`, `T11b-1`, `T11b-2`, `T11c″`, `T11c′`, `T11c‴`, `T11f` and
`T11g-1` have closed since). The "fourteen" this
section counted was itself a measurement of the uncorrected arm. After `T11a-3` fixed the derivation,
`T11c′`/`T11c‴` fixed the table, `T11f` catalogued the seventeen and `T11g-1` closed three, the
figure is **22 not green of 91 required, 11 BLOCKING-ALWAYS**. Eleven of the twenty-two are the
`T11g` ladder's (`-2`, `-3`, `-4`), six are `T11d`'s V-RUN pair, and the sentence this section used
to carry — _"eleven are asserted somewhere and recorded nowhere and only two are unbuilt"_ — was
false. `T11g-1` audited it: **one** was asserted and unrecorded, twelve were unbuilt.

---

## P9-T11a — the ratchet arm, built · 2026-07-31 · ✅

Planning defect 4's unbuilt half, shipped. `dev/tests/phase-ratchet-is-asserted.py` derives the
phase's required check set instead of remembering it, and asks `verification/results.csv` for a green
row per member.

**The denominator was wrong, and the check is what corrected it.** The audit above counted against
phase-9.md's own hand-written 55-ID acceptance table and reported **14 unasserted, 7
BLOCKING-ALWAYS**. Deriving the requirement properly — expanding each suite named in 09 §10's phase-9
row against the §6 catalog, honouring the `(L1)` qualifier on V-GAT — yields **70** IDs, and the
union with the table yields **75**. Against that denominator the gap is:

| Population                                                       | Count  |
| ---------------------------------------------------------------- | ------ |
| Required by 09 §10 ∪ phase-9.md                                  | **75** |
| Green (`**pass**` with a non-empty `evidence_ref`)               | 37     |
| Not green                                                        | **38** |
| …of those, BLOCKING-ALWAYS (09 §9.6 forbids deferral)            | **17** |
| Required by 09 §10 and absent from phase-9.md's acceptance table | **20** |

The twenty the acceptance table omits are V-BRK-018/019/020/022/023/024/025/026/027/028/029/030/031/032, V-GAT-005, V-GAT-011, V-GAT-014, V-GAT-016, V-REV-010 and V-REV-011 — planning
defect 4's exact shape, one phase later, found by the check written to end it. That is property 4,
and it is the reason the check reads 09 §10 rather than trusting the acceptance table: a table can
only omit what nobody re-derives. **Property 4 is orthogonal to property 2** — 15 of the 20 are
already green, proved by work no Accept bullet ever named — so it measures the acceptance table's
completeness, not the phase's. The five that are both omitted and not green are V-BRK-019,
V-GAT-005, V-GAT-011, V-GAT-014 and V-GAT-016.

**Property 4 read the whole file for one draft, and the control caught it.** The first implementation
computed `ratchet - CHECK_ID.findall(phase_text)` and reported **4**, because sixteen of the twenty
happen to be mentioned somewhere in this file's prose. Writing _this section_ — a paragraph naming
four of them while explaining that nothing asserts them — then took the count to zero and turned control
case 8 red. An ID named in a paragraph about how it is unasserted is not an ID bound to an acceptance
bullet, and counting it would have been [[LSN-019]] committed inside the check written to end
[[LSN-019]]'s previous recurrence. The property now reads the acceptance table, and the future-tree
case injects its IDs **inside** that section rather than appending them to the file, because
appending would satisfy a whole-file grep and not the property.

**What it asserts.** Four properties, all in `scan_text()`: (1) the required set is derived and both
sources parsed to something — a parse that silently matches nothing scores every unrun check as
satisfied ([[LSN-048]]); (2) every member has a `pass` row carrying an `evidence_ref`, because 09
§9.4 records a pass without one as `skipped`; (3) BLOCKING-ALWAYS members are counted separately,
because "not green" and "not green and undeferrable" are different facts and one number hides the
second inside the first; (4) the phase file's acceptance **table** does not under-name its own ratchet.

**What it refuses to do is the load-bearing part.** It does not infer coverage from a file naming a
check ID. `git grep V-ISO-001` finds `pair_netpol.go:68` and `pair_netpol_test.go:35`, and **both
hits exist to disclaim the check** — they say V-ISO-001/002 ask whether a packet is dropped, which is
L2 and belongs to P9-T9. A grep-based notion of "asserted" would have counted them as coverage and
scored the gap smaller than it is. The naming scan survives only as an explicitly unweighted `hint`
column that no verdict reads.

**Where it runs.** Section **J** of `dev/verify/verify-phase9.sh`. Deliberately **not** on
`dev/L0-CHAIN.txt`: the chain is a required PR check, and an arm that stays red until an entire phase
closes reddens every unrelated commit's CI, destroying the per-commit attribution CHECKPOINT exists
to produce ([[LSN-055]]). What keeps it from being quietly dropped is the pair — the `--negative-control`
**is** on the chain (line 48), and `invariants-gate.py`'s new
`check_phase_gate_runs_its_own_ratchet` asserts at L0 that every non-grandfathered
`verify-phase<N>.sh` invokes the runner, with `--phase <N>` matching its own number, on an
uncommented line, reaching a `bad ` arm within twelve lines. `grandfathered` is an explicit list of
phases 2–8, not a floor, so phase 10's gate is covered the day its file is created.

**The future tree ([[LSN-053]]).** This is a check split from the implementation whose absence
motivated it, so green-on-today is half the evidence. Control case 5 synthesises the tree T11b–T11d
will build — every required ID green, the phase file naming all 75 — and asserts the check **passes**
there. Cases 6–8 then perturb that future tree one property at a time: a BLOCKING-ALWAYS demotion, an
emptied `evidence_ref`, and a phase file back to under-naming. Without them, T11d's first green run
would arrive looking like "my implementation broke the check", and the cheapest diff to green would
be to edit the check.

| Artifact                                 | State                                                              |
| ---------------------------------------- | ------------------------------------------------------------------ |
| `dev/tests/phase-ratchet-is-asserted.py` | new · `--phase 9` → **rc 1**, the worklist above · control **8/8** |
| `dev/verify/verify-phase9.sh`            | section J wired, `bash -n` clean                                   |
| `dev/tests/invariants-gate.py`           | `check_phase_gate_runs_its_own_ratchet` → **31/31**                |
| `dev/test_invariants_gate.py`            | +11 named tests (9 gate-arm, 2 runner) → **88 OK**                 |
| `dev/L0-CHAIN.txt`                       | 47 → **48** lines — the control, not the check                     |
| `dev/assertion-baseline.json`            | wound for the 11 new tests ([[LSN-056]])                           |

Guardrail 9 holds structurally: this unit ships no implementation, and T11b–T11d ship no change to
this arm. Its redness is not a defect to fix — it is the worklist, and it goes green when the
worklist is done.

---

## P9-T11b-1 — chaos for the pair · 2026-07-31 · ✅

**V-ISO-001 and V-ISO-002 at L2, both BLOCKING-ALWAYS, both green.** The plan row was weighted
`large` and was split under `harness-run` §2 Sizing: T11b-1 is the chaos suite, T11b-2 is CH6.

### What was actually wrong

`dev/verify/chaos-suite.sh` contained the word **broker** zero times. It is a Phase 6 artifact and it
predates the pair, so C1 asserted "a running pod continues" against a stand-in and "no reconcile"
against the agent's Deployment, and C2 asserted the agent's Deployment was relaunched. Both rows it
was supposed to carry are about the half it did not know existed:

- **V-ISO-001** — "agents **and brokers** keep executing; no new reconciles"
- **V-ISO-002** — "relaunches **both** workloads, rebinds **both** SAs"

A suite cannot fail on a clause it does not mention. Both rows would have gone green at the milestone
on the strength of a suite that could not have detected either failure.

### The arms added

| Arm           | Claim                                                                                         | Row       |
| ------------- | --------------------------------------------------------------------------------------------- | --------- |
| **C1(i-b)**   | the deployed **broker pod** stays Ready across the whole controller outage                    | V-ISO-001 |
| **C1(ii-b)**  | the deleted **broker Deployment** is not recreated while the controller is down               | V-ISO-001 |
| **C1(iii-b)** | on restart the **broker** Deployment comes back too — the pair resumes, not half of it        | V-ISO-001 |
| **C2(i-b)**   | a broker Deployment deleted under a **live** controller is recreated, ownerRef = the CR       | V-ISO-002 |
| **C2(i-c)**   | both relaunched Deployments **rebind** the SAs the CR names, both existing, neither `default` | V-ISO-002 |

**Three decisions worth their own sentence.**

_The continuity claim is made against the real broker pod, not a stand-in._ The suite's D1 fixture
rule says reconcile-behaviour uses the real CR and pod-continuity uses lightweight stand-ins, because
the agent pod may never be schedulable. The broker is the exception and it is worth taking: its image
is the operator's own and it reaches 1/1 Ready in ~50 s on the scratch cluster, so "brokers keep
executing" is asserted on a broker rather than on a proxy for one.

_C1(i-b) and C1(ii-b) cannot share a window._ Deleting the broker Deployment garbage-collects the pod
that (i-b) spends 20 s asserting is stable. They are two blocks in a fixed order for that reason, and
the comment at the site says so, because the natural cleanup is to merge them.

_Both SA expectations are read back off the CR, never spelled out in the suite._ The reader SA comes
from `spec.security.serviceAccountName` and the actor SA from `status.broker.actorServiceAccount`,
which is where `agent_controller.go` publishes the name it resolved. Writing
`cluster-admin-cluster-a-actor` into the check would have produced an arm that keeps passing after
the operator stops deriving the binding — and deriving it is the entire property.

### The suite was on no chain at all

`chaos-suite.sh` was absent from `dev/L2-CHAIN.txt`. 09 §10 puts V-ISO-001/002/006 in Phase 9's
ratchet; a ratchet is the promise that a suite which entered it never leaves; and the only suite that
could keep that promise for CH1 and CH2 ran when somebody remembered it. Now listed, after the broker
suites (it needs the pair deployed) and before the gate.

### Non-vacuity

`dev/mutate.sh` over `broker_manifests.go`, mutator written to a file so the needle never touches a
shell ([[LSN-049]]), with the applier asserting the edit landed before anything ran. The broker pod's
`ServiceAccountName: actorServiceAccountName(agent)` became `"default"`; operator rebuilt and
redeployed by digest; suite re-run:

```
FAIL: C2(i-c): the actor binding is 'default' (expected 'cluster-admin-cluster-a-actor',
      cluster-admin-cluster-a-broker has 'default') — an unbound pod, not a rebound one (V-ISO-002)
```

Caught **by the arm that targets it, matched by that arm's own needle** ([[LSN-035]]), and
discriminated: C1(ii-b), C1(iii-b) and C2(i-b) stayed green, because they are object-level claims the
SA does not touch. C1(i-b) went red as collateral — a broker pod bound to `default` never becomes
available — which is honest, not a second catch. Source restored by byte-copy and the clean operator
redeployed before the final run.

### The gateway pod is in ImagePullBackOff, and it is not a defect

Mid-unit the gateway pod sat at `Init:0/1` with `wait-for-broker` logging `context deadline exceeded`
against the broker's FQDN, and the first diagnosis — that `<agent>-to-broker` is an Egress policy
with no DNS rule, so the reader cannot resolve its own broker — **was wrong**. The init container
completed with exit 0 once the broker became Ready; the deadline was one retry against a broker still
starting. What actually stops the gateway is that the fixture CR names
`ghcr.io/gke-labs/kube-agents/cluster-admin-agent:v0.1.0`, which is not an image this repo builds or
pushes. No assertion here depends on a Ready gateway. **P9-T11d does** — "both startup orders
converging" cannot be observed on a pod that never starts — so that unit begins by pointing the
fixture at a real image, and should not begin by re-diagnosing the NetworkPolicy.

### An invariant caught this unit's own race

`invariants-gate.py`'s P9 arm — `.status` reads are polled, not slept on — failed the first draft at
`chaos-suite.sh:481`: `status.broker.actorServiceAccount` was read once, immediately after C2(i-b)
deleted the broker Deployment and triggered the reconcile that writes it. Losing that race yields an
empty string, one branch away from being printed as _"the CR publishes no actor ServiceAccount
name"_ — a controller bug, reported because a read arrived early. Now a bounded poll.

| Artifact                    | Change                                                                  |
| --------------------------- | ----------------------------------------------------------------------- |
| `dev/verify/chaos-suite.sh` | +5 arms, `REAL_BROKER`, `wait_deploy_available`, the polled status read |
| `dev/L2-CHAIN.txt`          | 22 → **23** lines — `chaos-suite.sh`, listed for the first time         |
| `verification/results.csv`  | +1 row — V-ISO-001, V-ISO-002 at L2, `**pass**`                         |

### And the ratchet arm did not credit the result

Appending the row above moved nothing: the arm still printed 38 not green, 17 BLOCKING-ALWAYS. Its
`parse_results` keys on the raw `check_id` cell, so `V-ISO-001, V-ISO-002` is filed under that literal
string and matches neither ID. **38 of the 159 rows in `results.csv` group IDs that way** — it is the
file's dominant convention when one suite proves several rows, not an anomaly this unit introduced.

The under-count is material and it runs in the safe direction, which is why nobody noticed:

|                           | printed | true   |
| ------------------------- | ------- | ------ |
| required (09 §10 ∪ table) | 75      | 75     |
| not green                 | **38**  | **28** |
| of those BLOCKING-ALWAYS  | **17**  | **12** |

Ten IDs are falsely reported unasserted: V-BRK-002, V-BRK-015, V-GAT-001, V-GAT-010, V-GAT-011,
V-GAT-017, V-GAT-021, V-ISO-001, V-ISO-002, V-REV-008. The 38/17 figure recorded at `f068d82` is
therefore wrong in the same direction as the 14/7 it replaced — a hand-copied table gave a number too
small, and a parser that cannot read its own file's convention gives one too large.

**It is not fixed here.** Two of the ten are this unit's own rows, so repairing the check in the unit
whose result it would credit is Guardrail 9 in its exact form: _"the smallest diff to green is editing
the check"_. Scheduled as **`P9-T11a-2`**, which also owes the control a grouped-row case — the
existing eight only ever synthesise single-ID rows, which is precisely why the control was blind to
this.

**Resume at `harness-run`, unit `P9-T11a-2`** — then `P9-T11b-2` (V-ISO-006, binding
`broker-refuse-l2.sh` arm B to the ID it already proves).

---

## P9-T11a-2 — the cell is not the key · 2026-07-31 · ✅

**V-MET-013, V-MET-014 at L0.** One line of the fix, and the whole of the unit is why it was not one
line of `T11a`.

`parse_results` filed each results row under its raw `check_id` cell. **36 of the 160 rows name more
than one ID** — one suite run proves several catalog rows and gets one row citing one evidence
reference, which is the file's convention for a suite run and not an anomaly. So
`"V-ISO-001, V-ISO-002"` became a key that matched neither ID, and the arm reported both as never
asserted on the morning after they went green at L2.

| Population               | Printed | True   |
| ------------------------ | ------- | ------ |
| required (75)            | 75      | 75     |
| green                    | 37      | **47** |
| not green                | 38      | **28** |
| of those BLOCKING-ALWAYS | 17      | **12** |

The ten it had accused — V-BRK-002, V-BRK-015, V-GAT-001, V-GAT-010, V-GAT-011, V-GAT-017,
V-GAT-021, V-ISO-001, V-ISO-002, V-REV-008 — are green and always were. **A check written to find
unrun work that invents ten pieces of it is worse than no check**, because the ten are
indistinguishable from the twenty-eight that are real, and the natural response to the list is to go
and re-run things that have already been run.

**What the fix decides, beyond splitting the cell.** Two suffixes appear in that column and they are
not the same kind of thing. `(regression)` marks a re-run; `¬` is 09 §6's _negative-control
mandatory_ marker, copied off the catalog row — a property of the **check**, not a claim that the
row records only a control run. Both are stripped by the ID pattern, and for `V-CTR-002 ¬` that is
correct: it is a result row for V-CTR-002. A cell naming no ID at all (`(L0 mechanization)`)
contributes nothing, which is exactly what it did before under a key nothing could look up.

**Why the control could not see this, which is the more interesting half.** All eight of `T11a`'s
cases synthesised **one ID per row**. The control was therefore auditing the check against a shape
its real input predominantly does not have — [[LSN-060]]'s family (the control skipped the statement
under test), arriving through the _synthesiser_ rather than through a skipped statement. Three cases
added, 8 → **11**, all three built on a `_synthesise_green_grouped` that writes the future tree the
way `results.csv` is actually written, four IDs to a cell:

| #   | Case                                                                      | Requires                                           |
| --- | ------------------------------------------------------------------------- | -------------------------------------------------- |
| 9   | the future tree, IDs grouped one row per suite run                        | **PASS** — a grouped row credits every ID it names |
| 10  | one ID dropped from a grouped cell, cellmates untouched                   | caught by property 2                               |
| 11  | a grouped row demoted to `**finding**`, taking its BLOCKING-ALWAYS member | caught by property 3                               |

Case 10 is the guard against over-correcting: a split that credited by suite prefix, or that read
the whole row, would pass 9 and fail 10.

**The control's own victim locator had to change with it,** and that is a finding in miniature.
`demote` and `strip_evidence` matched their victim by `r[2] == check_id`. Against a grouped cell that
comparison is simply false, so cases 10 and 11 would have perturbed **nothing** and scored their
untouched input as an escape — a hole reported in the check that is really a hole in the control.
Both now locate by _the cell names this ID_.

**Non-vacuity, and it is discrimination rather than detection.** `dev/mutate.sh` with the mutator in
a file ([[LSN-049]]), each mutant caught by its own case and by no other:

| Mutant                                                                    | Result                                                                        |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| **M1** `parse_results` back to keying on the raw cell                     | **10/11** — case 9 alone reddens; 1–8, 10, 11 unmoved                         |
| **M2** the victim locator back to `==` instead of `in CHECK_ID.findall()` | **9/11** — cases 10 and 11 ESCAPE on an unperturbed input; case 9 stays green |

**And the sweep could not be configured, only run.** `harness-run` §5 wants it through
`dev/mutate.py` against a committed `verification/mutants/<CHECK-ID>.json`; that runner knows two
suite kinds, `go` and `unittest`, and the catcher here is the check's own `--negative-control`, which
is neither. So this unit fell back to `dev/mutate.sh` and hand-wrote an applier — the second unit in
two to do so, after `T11b-1`'s broker-SA mutant. That is the re-authoring LSN-047, LSN-048 and
LSN-049 were each paid for once, so it is **an inbox item rather than a paragraph** (`BACKLOG.md`,
added 2026-07-31): a `"kind": "command"` suite whose catch condition is a needle in the command's
output, which is what rule 5 already demands of the Go path.

**Guardrail 9 held on both sides.** `T11b-1` found this and did not fix it, because two of the ten
were its own rows. `T11a-2` fixes it and ships no implementation, so nothing in this unit can be
credited by the arm it repairs.

That last clause is measured, not asserted, because this unit's own results row is itself a grouped
cell (`V-MET-013, V-MET-014`) and is therefore exactly the shape the fix makes readable. Re-scanning
with the row removed gives the identical verdict — **28 not green, 12 BLOCKING-ALWAYS**, both IDs
green either way — because prior rows had already proved them. The fix credits nothing of its own.

Gate: control **11/11** · live arm rc 1 printing **28 / 12** (by construction — 28 checks are
genuinely unrun, and its redness is the T11b–T11d worklist) · `invariants-gate.py` 31/31 ·
`unittest discover dev` 397 OK · L0 chain 48/48.

**Resume at `harness-run`, unit `P9-T11b-2`** — V-ISO-006, binding `broker-refuse-l2.sh` arm B to the
ID it already proves.

---

## P9-T11b-2 — CH6, the journal half · 2026-07-31 · ✅

**V-ISO-006 at L2, BLOCKING-ALWAYS, green.** The plan row said _bind, cross-reference, run, record_ —
`broker-refuse-l2.sh` arm B already induced the exact fault and asserted the exact property, and
nothing named the ID, so the row had no claimant and no `results.csv` line. The unit did that, and
then did one thing the plan row did not ask for: it added **arm C**.

### Why the plan row was not the whole unit

05 §8 CH6 is five clauses, not one:

> **CH6 — Journal store down.** Make `ActionRecord` writes fail … The broker **refuses to execute**
> rather than executing unjournaled; auto-brake pauses the agent; the audit log shows zero mutations
> by that actor identity during the window; the failure is reported to humans. **Restoring the
> journal restores service without a broker restart.**

Arm B asserts the refusal, the zero mutations and the report. Executing the plan row literally would
have written a green V-ISO-006 with the last clause unasserted — and that clause is the one that
separates _refuses_ from _bricks_. A broker that latches the fault and never recovers passes every
assertion arm B makes. Arm C is that clause, and the code it needs already existed: the restore lived
inside `cleanup()`, where its result was discarded. The change was to make it evidence.

### The arms added

| Arm     | Claim                                                                                           |
| ------- | ----------------------------------------------------------------------------------------------- |
| **C-1** | after the grant is restored the same envelope is **accepted** (202), not still 503              |
| **C-2** | an `ActionRecord` now **exists** for the restored run's trace — service, not just a status code |
| **C-3** | the **same broker pod** served both, at the same `restartCount` — recovery without a restart    |

C-3 checks the pod **name first** and the restart count second, so a replacement is reported as a
replacement rather than surfacing as a puzzling count mismatch, and `POD_AFTER` is re-resolved by
ownership (`p3_pod_of_deploy`) so a pod that vanished lands in the REPLACED arm and not in the
"could not observe" one.

**A failed restore is `deferred` (rc 3), not red.** If the grant does not come back, arm C is
submitting into the same fault arm B just measured and asserting the opposite outcome: that is the
experiment failing, not the broker ([[LSN-026]]). rc 3, with the blocker named.

**The third probe scenario is not `journal-gone` run twice.** `broker_refuse_probe.py` gains
`journal-restored`, whose operations are byte-identical to `journal-gone`'s and deliberately so — the
recovery claim is only worth something if the thing that succeeds afterwards is the thing that was
refused before. What differs is `intent` and `rationale`, the two strings that land verbatim in the
ActionRecord this run actually writes.

### The auto-pause clause, corrected at the site

The header used to say the auto-pause consumer did not exist. That was true when written and is not
now — `P9-T9c-1` shipped it. The clause still cannot be asserted **in this fault**, for a different
and better reason: the pause is recorded **on the ActionRecord** (`escalate.Recorder.record` Gets
`journal.RecordName(actionID)`), and in this fault there is no record to put it on, because
`StoreRejectionJournal.Reject` is the write that just failed. `server.go`'s `autoPause` says so
itself and gives up — _"a refusal asked for an auto-pause and there is no record to put it on; the
agent stays live"_. So B-3 reading zero and the pause being unobservable are the same fact, not two
gaps. The paragraph now cites the lines instead of asserting the shape.

### Non-vacuity, and the gap the control admits

`--negative-control` went **25/25 → 34/34** with nine C-cases: baseline green, plus latched-503,
missing status, wrong refusal reason, accepted-but-unjournaled, unknown count, pod replaced,
container restarted, pod unobserved.

The control **synthesises the four pod strings**, so it proves the arm discriminates and says nothing
about whether `broker_restarts` and `p3_pod_of_deploy` read the pod that actually served the request.
That gap is written into the suite's own "NEGATIVE CONTROL DOES NOT EXERCISE" section, and then
closed live: mutant **M1** deletes the broker pod between the fault and the restore. Service still
returns — a fresh broker with a restored grant accepts and journals — so **C-1 and C-2 stayed green
and C-3 alone reddened by its own needle**, naming `…-k6pkd` before and `…-njdk2` after. That is
precisely the run a suite without C-3 would have printed PROVEN for.

### CH6 is not in the chaos suite, and that is not a gap

`chaos-suite.sh` gains a cross-reference block rather than a CH6 arm. Re-staging the fault there
would produce a second, thinner copy of a suite that already exists — and the copy is the one that
rots. The block names `dev/verify/broker-refuse-l2.sh` arms B and C and says why.

### Gate

`bash dev/verify/broker-refuse-l2.sh gke-scratch-kube-agents-dev` → **rc 0, 17/17**, banner
`PROVEN: V-BRK-018 · V-ISO-006 (05 §8 CH6) at L2 · the journal half of Phase 9 acceptance (d)`.
Arm B: 503 `journal-unavailable`, **0** records for trace `e47e8e15…`. Arm C: **202**, actionId
`01KYWDZK1DMFSC6XQ95T9AAW6V`, **1** record for trace `e8a2cdae…`, pod
`platform-agent-broker-66fd9d45ff-rxg45` at `broker=0` on both sides.

P1 green on **both** binaries — `k8s-operator@sha256:a7decacc29cb` and
`kage-broker@sha256:cc49feaab631`, both rebuilt at `dev-1190585-dirty`. The first two live runs failed
P1, once per image: `reload-images.sh operator` does not rebuild the broker, and there is no
`deploy/kubeagents-broker` to `set image` on — `reload-images.sh broker` sets
`KUBEAGENTS_BROKER_IMAGE` on the **controller**, which renders one broker per Agent CR.

Control **34/34** · `invariants-gate.py` **31/31** (baseline wound for `$c_status`, [[LSN-056]]) ·
L0 chain 48 clean · `unittest discover dev` **397 OK** · `make validate` clean · ratchet arm
**27 not green / 11 BLOCKING-ALWAYS**, down from 28 / 12 · `results.csv` 161 → **162** rows.

**Resume at `harness-run`, unit `P9-T11c`** — V-BRK-001, V-BRK-004, V-BRK-016 and V-REV-009 at L2,
the four unasserted BLOCKING-ALWAYS broker/undo rows. Weighted `large`; expect to split it.

---

## P9-T11a-3 — the column the ratchet was not reading · 2026-07-31 · ✅

Selected instead of `P9-T11c`, because researching `P9-T11c` found the defect that decides what
`P9-T11c` even is: **all four of its check IDs carry 09 §6 Phase `10`.**

### What was wrong

`dev/tests/phase-ratchet-is-asserted.py` derived the phase requirement from 09 §10's ratchet table
and threw away two things 09 says explicitly.

**It ignored 09 §6's Phase column.** The catalog's preamble calls each row "the assertion in brief,
the spec section that owns the rationale, the level, and **the roadmap phase by which it must be
green**". A bare suite name in a §10 cell expanded to every member of that suite, so phase 9 —
whose 07 §2 definition is _"no write authority anywhere"_ — was being asked for V-BRK-016
(post-execution journal failure: the write lands and the record cannot be completed), V-BRK-003
(real audit-log writes), V-BRK-004 (a `vap-agent-scope`-class admission policy that does not exist
until P10-T1), and V-RUN-014, whose own catalog row dates it to **phase 15**.
`dev/verify/broker-execute-l2.sh` had already written the conclusion down without anyone acting on
it: _"V-BRK-019 (the field manager string) is not observable from a shadow."_

**It read one row.** §10 opens: _"Which suites must be green at the end of each roadmap phase — and
**stay** green thereafter. Once a suite enters the ratchet it never leaves."_ The arm read only the
row for the phase on the command line, so phase 9 did not require V-CTN, V-CTR, V-CMP or V-MET at
all, though all four entered at phase 8.

### Why the Phase column is the right reading, and not a convenient one

Three confirmations, none of which depend on wanting the answer:

- **§10's later rows re-name members of suites already in the ratchet** — V-REV-008 at 14,
  V-ADV-003/005 at 13, V-CTN-010/013/018/019 at 11. Under whole-suite expansion every one of those
  is dead text, because those suites entered at 9, 10 and 8.
- **The Phase column reconstructs §10's own prose qualifier, exactly.** `V-CTN (read-side)` at 8 and
  `V-CTN (write-side)` at 10 partition the suite. Filtering V-CTN by `phase <= 8` yields precisely
  the seven reader/attenuation/cardinality rows; `phase == 10` yields precisely the fifteen
  actor-write and forbidden-rule rows. Two encodings of one partition, and only one is
  machine-readable. The arm was discarding the machine-readable one and the prose one both.
- The read that makes the gate _cheaper_ is the one the arm already had.

### The shape of the correction

The two halves pull in opposite directions, which is the point:

|                                   |                                                                                                                         |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Ratchet (09 §10)                  | **70 → 80**                                                                                                             |
| Required (§10 ∪ acceptance table) | 75 → **98**                                                                                                             |
| Not green                         | 27 → **34**                                                                                                             |
| …of those BLOCKING-ALWAYS         | 11 → **19**                                                                                                             |
| Dropped by the phase filter       | **21** — V-BRK-001/003/004/016/019/020, V-GAT-005/011/014/016/019/021, V-REV-002/005/006/007/008/009, V-RUN-006/013/014 |
| Added by accumulation             | **31** — V-CTN read-side (8), V-CTR core (12), the V-MET meta-suite (11)                                                |

A change that made the gate cheaper would not have that shape. The gate got harder by seven checks
and eight BLOCKING-ALWAYS ones.

An ID §10 names **outright** is never phase-filtered: the explicit form is how §10 pulls a member
forward or holds one back, and filtering it by the column it exists to override would make the
explicit form unable to mean anything. At phase 9 that branch is inert — §10's only explicit IDs by
then are `V-ISO-001/002/006` and §6 already dates all three to 9 — so the control cannot stage a
case for it and does not pretend to.

A catalog row with **no** Phase cell — the nine V-MET rows of §11, meta-checks that apply at every
phase — is required wherever its suite is named. That exemption can only keep a check _in_, and it
is counted and printed, never applied in silence.

### The filter reports itself

The phase filter is the only part of the derivation that makes the required set **smaller**, so it
is the only part that can buy a green by being wrong. It prints what it removed and what it could
not classify, on the green run as well as the red one, and three of the control's twenty lines
assert that it does. A filter that removes 21 IDs and says nothing is indistinguishable from a
smaller spec.

### Control: 11 → 20

Five new cases and three note assertions. The five perturb 09 §6 and 09 §10 themselves, which no
earlier case did:

1. **Pull forward** — rewrite V-BRK-019's Phase cell to `9`. The future tree must go red naming it.
2. **Delete the Phase cell** — V-BRK-019 becomes undated. Still required, so still red.
3. **Push later, half one** — a tree green for everything except V-BRK-018 must go red naming it.
4. **Push later, half two** — the same tree, with §6 pushing V-BRK-018 to phase 99, must go
   **green**. This is the arm that proves the filter _removes_ rather than merely being consulted;
   without it, case 3 passes against a derivation that ignores the column.
5. **Carried forward** — a tree green for everything except V-CTN-037, which the phase-**8** row
   carries but §6 dates to **9**. Every accumulated row must filter against the phase under test,
   not against its own number.

Case 5 was added because the sweep found the hole. `M4` — each row filtered against `row_phase`
instead of `phase` — silently removed 11 IDs from the required set and the control was **19/19
green** through it.

The property-2/3/4 sentences carry counts and not IDs, so cases whose whole claim is _"THIS ID
became required"_ are matched against the named not-green population the report already prints.
Matching them on "something went red" would score every perturbation as catching every property
([[LSN-035]]).

### The control derives its victims from the code under test, on purpose

There is no synthesised stand-in for the derivation ([[LSN-060]]) — each victim is picked out of
`parse_ratchet`'s own output. The cost is that a defect in the derivation can empty the pool a case
needs, and an empty pool must never quietly shrink the control to the cases it can still stage. The
`pick` helper raises with the **case's own name**, so three of the four mutants below are caught by
a distinct message even though none of them reaches a case outcome.

### Non-vacuity: `dev/mutate.sh`, mutator in a file ([[LSN-049]]), 4/4 caught

| Mutant | The defect                                                                                                       | Caught by                                             |
| ------ | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **M1** | accumulation dropped — only this phase's row is read (the arm exactly as it stood)                               | `no victim for the prior-row case`                    |
| **M2** | the filter is consulted and never removes                                                                        | `no victim for the pull-forward case`                 |
| **M3** | an undated row is dropped instead of required — the reading that would exempt every V-MET check from every phase | case 2 ESCAPES: `phase cell for V-BRK-019 is deleted` |
| **M4** | each row filtered against **its own** phase instead of the phase under test                                      | `no victim for the carried-forward case`              |

Four different needles. `rc != 0` is not a catch.

One mutant was **discarded as equivalent** rather than recorded as a finding: reading the phase from
the _first_ integer-shaped cell instead of the last changes nothing, because on every 09 §6 row the
only bare-integer cell is the phase cell. A no-op mutant scored as an escape would have bought a
"strengthening" that measured nothing.

### What this unit deliberately did not do

`docs/build/phase-9.md`'s acceptance table names **16** IDs that 09 §6 dates after phase 9 —
V-BRK-001/003/004/016, V-GAT-019/021/022, V-REV-002/005/006/007/008/009, V-RUN-006/013/014 — and
four of those are the _"(ratchet only)"_ rows `T11a` added on the belief the ratchet demanded them.
The required set is the **union** of §10 and the table, so the table keeps every one of them
required and this unit's 34-not-green figure is unchanged by the correction that is coming.

That is the point. Correcting the table would move this unit's verdict as well as its measurement,
and a unit that both moves the line and reports crossing it is Guardrail 9 in its exact shape — the
same separation used for `T11a → T11a-2` and `T11b-1 → T11a-2`. The next unit owns the table, with
these numbers in hand.

### Gate

`python3 dev/tests/phase-ratchet-is-asserted.py --phase 9 --negative-control` → **20/20**, rc 0 ·
the live arm → rc 1, **34 of 98 not green, 19 BLOCKING-ALWAYS**, property 4 names **43** ·
`dev/tests/invariants-gate.py` **31/31** (baseline wound for the one new named test, [[LSN-056]]) ·
`python3 -m unittest discover dev` **398 OK** (was 397) · full `dev/L0-CHAIN.txt` **48/48** clean ·
`results.csv` 162 → **164** rows.

`dev/test_invariants_gate.py` gains
`test_the_ratchet_accumulates_prior_phases_and_honours_the_due_date`, asserting the two properties
on **named IDs** rather than on counts — a count assertion gets rewritten to whatever the code
produced the next time 09 gains a row.

**Resume at `harness-run`, unit `P9-T11c′`** — correct `docs/build/phase-9.md`'s acceptance table
against the Phase column: retarget the sixteen later-dated IDs, and rewrite the four
_"(ratchet only)"_ rows T11a added. Only then `P9-T11c` proper, which after that correction is
V-BRK-001 at most and possibly nothing.

---

## P9-T11c″ — a 20/20 control that only worked while the document stayed wrong · 2026-07-31 · ✅

**This unit was not planned. It was scheduled mid-CHECKPOINT, by `P9-T11c′` failing.**

`P9-T11c′` — the section below, which landed after this one — corrected this file's acceptance table
in one direction and wrote the other direction too: the 43 IDs the table omits. That paste was
written, run, and reverted, because the ratchet arm's own negative control went from 20/20 to
**unstageable**:

```
FAIL: negative control could not be staged: control: no victim for the push-later case
```

and two committed assertions in `dev/test_invariants_gate.py` went red behind it, the second masked
by the first.

### The coupling

Three of the five phase-filter cases `P9-T11a-3` added pick their victim from _"required by the
suite expansion and **not named by the acceptance table**"_. That pool is exactly the 43. Complete
the table and all three pools empty at once, and `pick()` — added in `T11a-3` precisely so an empty
pool could never quietly shrink the control — does what it was built to do and refuses to stage.

Repairing that surfaced two more instances of the same shape, neither of which was known going in:

| #   | Where                                               | What it did                                                                                                                                                                                                                                 |
| --- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `stage()`'s `name_in_table`                         | **Prepended** a synthetic row to the real acceptance section instead of **replacing** it, so every synthesised tree silently carried today's table as well. Invisible today; fatal once the table is complete.                              |
| 2   | the `push-later` and two sibling pools              | Filtered on `c not in table`, the term that empties them.                                                                                                                                                                                   |
| 3   | the case _"the phase file under-names its ratchet"_ | Staged **`phase_text` itself** and relied on it being wrong by 43 IDs. Passes today; **escapes** the moment the table is completed, then reports a hole in property 4 that is really the control describing a tree the repository has left. |

The general shape is one sentence: **a control whose cases borrow the live artifact is a control
that only works while that artifact stays wrong.** It reads 20/20 the whole time. That is the
V-MET-014 failure mode — a suite of vacuous passes reading green — with the vacuity scheduled to
arrive on a specific future commit rather than present today, which is strictly worse, because the
commit that exposes it is the commit that gets blamed for it.

### What changed

- `name_in_table` replaces the acceptance section. `parse_acceptance_table` stops at the next
  top-level `##`, so the replacement splices head + heading + synthetic row + the next `##` onward.
- The five victim pools get **one definition site**, `victim_pools(tbl)`, used for both staging and
  audit. An audit that re-states what it audits drifts away from it, and that drift is how the
  `c not in table` terms survived a reviewer twice.
- A **stageability guard** runs `victim_pools` against two synthetic tables on every ordinary run —
  a complete one and one with every later-dated ID retargeted out — and fails if any pool empties.
  It fires on the ordinary L0 run, not only on a tree nobody runs.
- The under-naming case synthesises its own under-naming instead of borrowing the document's.
- `phase_of()` uses `catalog.get`: `V-CMP-006` is named by the table and has no §6 row, so the first
  draft of the guard died on `KeyError` — finding (1) of `T11c′` biting inside the fix for it.

`_control_against()` in `dev/test_invariants_gate.py` hands the module a **temp repository** — the
real 09 spec, the real `results.csv`, and a synthesised phase-9.md — so the two future trees are
asserted as committed cases on every chain run rather than proved once in `/tmp`. That is
`harness-run` §4's requirement read literally, and it is what caught instances 2 and 3.

Two of that file's existing assertions were also decoupled from the artifact's current shape. A
floor of `> 40` set just under the table's current 41 is a fingerprint of the document, not a
property of the parser; it fell to `> 10`, and the six hardcoded `V-RUN-00n` membership assertions
went, replaced by a fixture-based test of the two parse behaviours the phase file's structure
actually depends on — a `###` subsection is still inside the acceptance section, and
`V-RUN-001…005` expands to five.

### The ordering, which is the finding

`P9-T11c′` was **complete and verified** when its artifact change reddened those assertions. The
cheapest path to green was a two-line edit to `dev/test_invariants_gate.py` in the same commit. That
is Guardrail 9 verbatim, so the artifact was preserved to `/tmp`, reverted, and **this unit was
built and landed first**.

Guardrail 9 forbids changing a check in the same unit as the implementation whose failure motivated
it. It does not say which of the two goes first, and the cheap reading is _"land the artifact, fix
the check next"_. That reading is wrong, and this is what shows it. With the artifact in, the check
unit's own justification becomes _"this assertion is red"_, the tree it must be green on is the only
tree available, and the smallest diff to green is a retune — Guardrail 9's exact pressure, arriving
one unit later wearing the face of a repair. Landing the check first inverts all of it: the
assertion is green when the unit starts, so the unit has to argue the property rather than the
symptom, and the future tree is genuinely absent, so it has to be synthesised and committed. Which
is [[LSN-053]] asked for from the other side, and is what caught instances 2 and 3.

**Where a check and its artifact must be split, the check is the earlier unit.** Recorded in the
ledger's decisions table as a candidate edit to `PROTOCOL.md` §10.1 for the next improvement pass.

### `P9-T11c″-b` — the fix for the coupling had the coupling in it

The three-tree claim above was made, and for the unit suite on `T11c′`'s tree it was **wrong**. It
was caught the only way it could be: `T11c′` re-landed its artifact and failed on the spot.

```
AssertionError: 39 not less than 39 : nothing to retarget -- the fixture proves nothing
```

Both future-tree fixtures, **and the stageability guard itself**, built their hypothetical
acceptance table as _"the live table minus what 09 §6 dates after this phase"_. That is a proper
subset today, so it looked fine. The moment `T11c′` performed exactly that subtraction on the real
document there was nothing left to subtract: the hypothetical collapsed onto the live table, the
fixture's own validity assertion fired, and the guard quietly went from auditing two trees to
auditing one. **Borrow-the-artifact, one level up, sitting inside the fix for borrow-the-artifact.**

- Both hypotheticals derive from **09 alone** — `complete` is the required set, `partial` is every
  other member of it. Neither reads `phase-9.md`.
- `assert_hypotheticals_distinct()` is lifted to module level and refuses two hypotheticals that
  are the same set. That is what makes _"derive from 09"_ checkable **today** rather than on a
  commit nobody has written yet, and it is the arm that catches the whole family.
- `_fixture_tables()` is one definition site and takes the module as a parameter.

The first sweep of the three new mutants **ESCAPED twice**, and neither was a `BROKEN` row: M9
because `_fixture_tables` called `_load_phase_ratchet()` internally and so returned a fresh module
that ignored the caller's patch, and M8 — remove the detector, restore the defect — because nothing
tested the detector. A detector whose only evidence is _"the thing it detects is not happening
today"_ can be deleted with every gate green. Both are caught now.

**What this says about the unit above.** Its three-tree verification was run as _"the control, on
three trees"_ and reported as _"the control and the suite, on three trees"_. The gap is small and it
is exactly where the defect was. Re-run per-tree, both, it holds.

### Gate

Verified on **three trees** — today's (required 98 · not green 34 · BLOCKING-ALWAYS 19), `T11c′`'s
(82 · 22 · 11) and `T11c‴`'s complete table (82 · 22 · 11, property 4 silent). Control **20/20** and
the unit suite **OK** on each, each run separately against that tree on disk.

`verification/mutants/V-MET-014.json` **9/9 caught** under `dev/mutate.py`'s existing
`"kind": "unittest"`: three mutants restore the coupling directly, one proves the stageability guard
is load-bearing on the ordinary run, one removes the guard as well to prove the committed future
tree is an independent backstop rather than decoration, two attack the acceptance parse, and three
were added by `T11c″-b` for the coupling that was still in the repair. `dev/assertion-baseline.json`
wound to 1367 named tests ([[LSN-056]]) · `invariants-gate.py` **31/31** ·
`unittest discover dev` OK · full `dev/L0-CHAIN.txt` **48/48** clean.

**B-012 is narrowed, not closed.** It asks for a `"kind": "command"` suite kind for a check whose
only catcher is its own `--negative-control`, and that suite kind still does not exist. This unit
found a third option its filing did not consider — give the control's catcher to a unittest, and the
existing `kind: unittest` hosts the sweep — and recorded it against the item for the pass to weigh
before building a runner kind for two units.

---

## P9-T11c′ — the sixteen that were not Phase 9's · 2026-07-31 · ✅

**Landed second, on top of `P9-T11c″`.** It was implemented first; see
[_The ordering, which is the finding_](#the-ordering-which-is-the-finding) above for why it was
reverted and re-landed rather than committed with the check repair it forced.

`P9-T11a-3` corrected the **derivation**: 09 §6's Phase column is authoritative over a 09 §10 suite
name, and every §10 row up to and including this one is in the phase's ratchet. It deliberately left
the **artifact** alone, because phase-9.md's acceptance table is the other half of the union the arm
computes, and a unit that both moves the line and reports crossing it is Guardrail 9 in its exact
shape. This unit is that other half.

### What changed

Sixteen IDs left the acceptance table because 09 §6 dates them to phase 10, 14 or 15. They are not
dropped: [_Retargeted out of Phase 9_](#retargeted-out-of-phase-9-by-09-6) records every one with
its due phase and the row it came from, and each keeps its ID, level, suite and BLOCKING-ALWAYS
class. Twelve of the sixteen are not yet green and become Phase 10's opening worklist.

|                                  | Before | After  |
| -------------------------------- | ------ | ------ |
| required (ratchet ∪ table)       | 98     | **82** |
| not green                        | 34     | **22** |
| of those, BLOCKING-ALWAYS        | 19     | **11** |
| table under-names the ratchet by | 43     | 43     |

**They are named outside the acceptance section, not inside it.** `parse_acceptance_table` reads
every `V-XXX-nnn` between the acceptance heading and the next `##`, so a paragraph explaining that
V-BRK-016 is postponed would go on requiring V-BRK-016 — [[LSN-019]] arriving inside the correction
that closes it, which is exactly the trap `P9-T11a` fell into when writing prose about four IDs took
its own count to zero. The retarget list is therefore its own `##` section. That is a structural
answer, not a stylistic one: the record and the requirement cannot share a parse.

### Why the other half of the correction is a separate unit

The table also **omits 43** IDs 09 §10 requires — the same defect mirrored, and completing it is one
paste. That paste is not in this commit. It was written here, run, and reverted, because it took the
ratchet arm's negative control from 20/20 to **unstageable** and reddened two committed assertions
behind it — the discovery that became [`P9-T11c″`](#p9-t11c--a-2020-control-that-only-worked-while-the-document-stayed-wrong--2026-07-31-),
which landed first and is written up above.

The smallest diff to green was _editing the check_, which is Guardrail 9 verbatim. So the ladder is
three units for one paste:

- **`P9-T11c′`** (this one) retargets the sixteen. ✅
- **`P9-T11c″`** repaired the control, check-only, green on today's tree and on two synthesised
  future ones. ✅ — **landed before this unit**, which is the point: a check split off under
  Guardrail 9 goes _first_, or its only available tree is the red one.
- **`P9-T11c‴`** completes the table, against a control that can already stage on it. ✅ — landed
  the same day, and the control that `T11c″` had already taught to stage a complete table stayed at
  20/20 through it, which is what a correctly-ordered split buys.

That is the price of the guardrail, and the guardrail is right: the alternative is a session in
which the control was edited to accommodate the artifact and nobody can afterwards say which one was
wrong. Here the control turned out to be wrong in **three** places, only one of which was visible
from this unit.

### The catalog gap this unit found

`V-CMP-006` is required by this table and has **no row in 09 §6**, which is how the arm ended up
reporting a required ID whose `phase` is `None` for a reason unrelated to §11. Counting properly:
**17 of the 251 check IDs 09 mentions have no §6 catalog row** — 14 V-CMP (001–008, 010, 011,
020–023) defined as prose bullets in §5, and **V-MET-010, V-MET-011, V-MET-012** defined in §14,
_"Verification of this document"_.

09 §6's preamble calls itself _"The authoritative index"_. Seventeen checks it does not index are
seventeen checks no suite-name expansion can ever reach — and both suites are in the phase-8 ratchet
row, so all seventeen are required at phase 9 and none of them is derivable. **Three are
BLOCKING-ALWAYS.** All three are in fact implemented (`dev/tests/spec-ids.py` for V-MET-010/012,
`verification/traceability.yaml` + its bidirectional lint for V-MET-011, built as P8-T10), so this
is a hole in the index and not in the work — but a BLOCKING-ALWAYS check that the gate cannot see is
a BLOCKING-ALWAYS check the gate cannot fail on, and the fact that these three happen to be green is
not something the gate established. Recorded as a `finding` row against V-MET-010; scheduled as
**`P9-T11f`**.

**Closed by `P9-T11f` on 2026-07-31.** All seventeen now have a catalog row — the fourteen V-CMP in
a new **§6.15**, the three V-MET appended to the §8 table where the rest of their suite lives — and
the §5 and §14 prose bullets dropped their bold markers so each ID keeps exactly one definition site
(V-MET-013 fails the build on a second). The gate grew by the ten of the seventeen whose due phase
is ≤ 9: required **82 → 91**.

### The other thing the corrected required set exposes

Of the 22 checks now not green, **ten have no row in `verification/results.csv` at all**:
V-CTN-001, V-CTN-004, V-CTN-012, V-CTN-015, V-CTN-016, V-CTN-017, V-CTR-003, V-MET-001, V-MET-008,
V-MET-009. Nine of the ten are BLOCKING-ALWAYS, and every one of them belongs to a suite that
entered the ratchet at **phase 8** — which closed. They are not unbuilt: V-CTN-012/017 and V-CTR-003
are asserted by `vap-agent-scope` tests and `dev/tests/` lints on the L0/L2 chains, and V-MET-001/
008/009 are `invariants-gate.py` arms. What is missing is the **record**, and 09 §9.4 is explicit
that the record is the evidence. A phase-8 ratchet declared green over ten members with no results
row is the same class of defect as this table's, one phase earlier, and it is only visible now
because the accumulation exists to look for it. These are runs to be made, not rows to be written —
scheduled as **`P9-T11g`**, ahead of the milestone.

### Gate

`python3 dev/tests/phase-ratchet-is-asserted.py --phase 9 --negative-control` → **20/20**, rc 0 ·
the live arm → rc 1, **22 of 82 not green, 11 BLOCKING-ALWAYS**, property 4 names **43** ·
`dev/tests/invariants-gate.py` **31/31** · `python3 -m unittest discover dev` **OK** · full
`dev/L0-CHAIN.txt` **48/48** clean.

The control's stageability guard, added by `T11c″`, is what makes that 20/20 mean something on this
tree: it re-runs all five victim pools against a synthesised complete table on every ordinary run,
so this unit's artifact change cannot quietly re-couple the control to the document it audits.
`T11c″`'s two committed future-tree tests were written against **exactly this artifact** and were
green before it landed — which is the whole reason it could land at all.

`P9-T11c‴` — the other half of this correction — is written up immediately below.

---

## P9-T11c‴ — the forty-three the table owed and never named · 2026-07-31 · ✅

The third rung of the ladder, and the one the other two were built to make safe. `T11a-3` corrected
the derivation, `T11c″` decoupled the control from the document, `T11c′` removed the sixteen IDs the
table demanded too early — and this unit adds the forty-three it required and never wrote down.

### What the defect actually was

Not a missing check. Every one of the forty-three was **already required and already gated**: they
reach the required set through 09 §10's suite names, which is why the arm's `required` figure is
unchanged at **82** across this commit. What was missing is the phase file's own account of them. A
required set assembled as `ratchet ∪ table` where the table names 39 of 82 leaves the phase file
saying two different things about what the phase owes, depending on which half you read — and the
half a human reads is the table.

That is planning defect 4 restated at the level of the artifact rather than the plan. §4's original
form was _"Accept (a)–(e) does not cover the ratchet"_, answered by adding explicit ratchet-only
rows. The answer was right and incomplete: nine rows were added and forty-three obligations were
not, and nothing measured the gap until `T11a` built property 4.

### What changed

Eight themed `_(ratchet only)_` rows, each carrying the level and target 09 §6 gives its members:

| Row                                             | IDs    | Level      |
| ----------------------------------------------- | ------ | ---------- |
| the broker pipeline's own properties            | 6      | L1         |
| the agent↔broker seam                           | 4      | L0, L1     |
| refusal beats partial work                      | 2      | L1, L2     |
| reversibility beyond the undo plan              | 2      | L1, L2     |
| containment, carried in by 09 §10's phase-8 row | 6      | L0, L2, L3 |
| a test-only RBAC grant never leaves `dev/`      | 1      | L0         |
| the CRD contract and the brake                  | 11     | L0, L1, L2 |
| the measurement suite                           | 11     | L0         |
| **total**                                       | **43** |            |

Themed rather than one flat list, because the rows are what a reader uses to understand what the
phase is closed against, and forty-three IDs in one cell is a list, not an account. The grouping is
09 §6's own — each row's members share a spec section and a level.

**`V-MET-001…009` is written as an ellipsis run**, which `parse_acceptance_table` expands
deliberately (the same handling `V-RUN-001…005` already relies on). The bold markers sit outside the
run — `**V-MET-001…009**`, never `**V-MET-001**…**009**` — because the expansion regex matches
`V-MET-001…009` as one token and the second form silently contributes only the two endpoints. Seven
IDs would have vanished into a formatting choice, and property 4 would have gone on naming them
while the table appeared to carry them.

### What did not change, and why that is the result

|                                  | Before | After  |
| -------------------------------- | ------ | ------ |
| required (ratchet ∪ table)       | 82     | **82** |
| not green                        | 22     | **22** |
| of those, BLOCKING-ALWAYS        | 11     | **11** |
| table under-names the ratchet by | 43     | **0**  |
| table names                      | 39     | **82** |

A unit whose entire artifact is documentation and whose gate figures are unchanged is exactly what
"the table was short and the ratchet was not" predicts. If any of the first three numbers had moved,
the claim would have been false and the paste wrong.

### Gate

`python3 dev/tests/phase-ratchet-is-asserted.py --phase 9 --negative-control` → **20/20**, rc 0 —
unchanged through the artifact edit, which is `T11c″`'s stageability guard doing the job it was built
for. The live arm → rc 1, **22 of 82 not green, 11 BLOCKING-ALWAYS, property 4 silent**; rc 1 is
correct and expected, and it is properties 2 and 3 that keep it red — those are `P9-T11g`'s and
`P9-T11d`'s work, not this unit's.

`dev/tests/invariants-gate.py` **31/31** · `python3 -m unittest discover dev` **OK** · full
`dev/L0-CHAIN.txt` clean · prettier over the branch diff.

**Resume at `harness-run`, unit `P9-T11f`** — the 17 check IDs 09 mentions with no §6 catalog row,
three of them BLOCKING-ALWAYS. Then `P9-T11g` (the ten unrecorded phase-8 ratchet members) and
`P9-T11d` (the workload pair at L2, which must start by pointing the fixture at an agent image this
repository actually builds).

---

## P9-T11f — the seventeen 09 defined only in prose · 2026-07-31 · ✅

The fourth rung, and the first one that makes the gate **bigger**. `T11a-3` corrected the derivation,
`T11c″` decoupled the control, `T11c′` removed the sixteen too-early IDs and `T11c‴` wrote down the
forty-three the table owed. All four moved the account of the required set without changing which
checks the project is obliged to. This unit changes that set: **82 → 91**.

### What the defect actually was

09 §6's preamble calls itself _"The authoritative index"_, and the phase ratchet is derived by
expanding a §10 suite name against it. Seventeen of the 251 check IDs the document defines had no §6
row — fourteen V-CMP as prose bullets in §5's three inventories, and V-MET-010/011/012 in §14,
_"Verification of this document"_. Both suites are in the phase-8 ratchet row, so all seventeen were
obligations of every phase from 8 on, and not one of them was reachable by the expansion. Three are
BLOCKING-ALWAYS.

The shape is worth naming because it is the opposite of the usual one. Nothing was unbuilt and
nothing was failing — the fourteen V-CMP have their rationale in §5, and V-MET-010/011/012 all run
today on the L0 chain. What was missing is the **index entry**, and an index entry is the only thing
a suite-name ratchet can see. A gate that cannot see a BLOCKING-ALWAYS check cannot fail on it, and
the fact that these happened to be green is not something the gate had established.

### Why the bullets had to give up their bold markers

`dev/tests/spec-ids.py` treats both a catalog row (`| V-CMP-001 | … |`) and a bold bullet
(`- **V-CMP-001** — …`) as a **definition site**, and V-MET-013 fails the build when an ID has more
than one. Adding a §6.15 row while leaving `- **V-CMP-001**` standing would have defined every one of
the fourteen twice. So all seventeen prose bullets were converted to plain backticks
(``- `V-CMP-001` — …``) first, and their `L2`/`L0` level annotations removed — the row is now the
single place level and due phase are stated, and the bullet is prose that reads it.

### Where each suite is catalogued, and why not both in one place

| Suite         | Catalogued in                     | Because                                                          |
| ------------- | --------------------------------- | ---------------------------------------------------------------- |
| V-CMP-001…023 | **new §6.15**                     | its rationale is §5's inventories; §6.15 indexes, never restates |
| V-CMP-024     | §6.14 (unchanged)                 | the coverage audit that found it put it there                    |
| V-MET-010/012 | the **§8** table, after V-MET-007 | next to the traceability obligation they police, with 001–009    |
| V-MET-011     | the **§8** table                  | same                                                             |
| V-MET-013/014 | §6.14 (unchanged)                 | arrived with the coverage audit                                  |

`parse_catalog` scans the **whole** document, so §6 vs §8 placement changes nothing the ratchet
reads — it is a legibility choice, and it is recorded in §6's preamble so the next reader does not
have to rediscover that "the authoritative index" is authoritative in two places.

### The due phases were argued, not assigned

The Phase cell is the only thing in a new row that moves the gate, so each of the fourteen was dated
from the spec rather than from convenience. The rule the §6.15 preamble states: **a completeness
check comes due when its population completes**, because an inventory check over a population that
does not exist yet is vacuous, not passing.

| Due | IDs                      | The population that has to exist first         |
| --- | ------------------------ | ---------------------------------------------- |
| 8   | V-CMP-002, 003, 011, 020 | images, manifests, the CRD schema, the tiers   |
| 9   | V-CMP-006, 007, 008      | the broker's identities and the install render |
| 12  | V-CMP-010                | the mesh contract                              |
| 13  | V-CMP-005, 021, 022      | V-PRO and the provisioning pair                |
| 14  | V-CMP-001, 004           | the observability pipeline and its exercisers  |
| 15  | V-CMP-023                | the ChatOps verb surface                       |

V-MET-010/011/012 carry **no Phase cell**, because the §8 table has no such column — they parse as
`undated`, which the arm reads as _required at every phase naming the suite_, i.e. every phase from
8 on. That is the honest reading of a check on the specification itself, and §8 now says so in
prose rather than leaving it to be inferred from a missing column.

### What moved, and this time the movement is the result

|                              | Before | After the catalog edit | After this unit's runs |
| ---------------------------- | ------ | ---------------------- | ---------------------- |
| required (ratchet ∪ table)   | 82     | **91**                 | **91**                 |
| not green                    | 22     | 26                     | **25**                 |
| of those, BLOCKING-ALWAYS    | 11     | 13                     | **12**                 |
| table under-names ratchet by | 0      | **0**                  | **0**                  |

Ten of the seventeen are due at or before phase 9; the required set grew by **nine**, because
V-CMP-006 was already in it through the acceptance table — the very anomaly `T11c′` reported as _"a
required ID whose phase is `None`"_. Of the ten, five were already green (V-CMP-002, 003, 007, 008,
V-MET-011) and four were not: **V-CMP-011, V-CMP-020, V-MET-010, V-MET-012**, the last two
BLOCKING-ALWAYS.

The middle column is the honest one to quote as the unit's effect on the gate, and the right column
is where it closed: V-MET-010 is one of this unit's own claimed checks, its property is
`dev/tests/spec-ids.py`, and running it is what closes the `finding` row `T11c′` opened against it.
V-MET-012 is the same script and passes today, so it needs a results row and not a build; V-CMP-011
and V-CMP-020 have **no implementation anywhere in the tree**. Those three go to `P9-T11g`.

Property 4 stayed silent only because the acceptance table was extended in the same unit: ten new
ratchet members would otherwise have left it under-naming again. Two rows absorbed them — the
measurement row widened to `**V-MET-001…014**` (markers outside the ellipsis run, per `T11c‴`) and a
new completeness row carrying the seven V-CMP due by 9. The `_(carried, not ratchet)_ V-CMP-006` row
is gone: V-CMP-006 has a dated catalog row now and is genuinely ratchet.

### One truth defect fixed, one left alone

§14 still claimed V-MET-011 was _"not implemented; scheduled as P8-T10"_. It landed as P8-T10 in
PR #29 (`ead358e`) and `verification/results.csv` records it **pass** on 2026-07-27. Corrected, and
the closing paragraph now says all three run on the L0 chain.

`dev/tests/phase-ratchet-is-asserted.py` carries a comment reading _"§11's V-MET table has no phase
cell at all"_ — it is §8, not §11. Left alone deliberately: editing a check in the same unit that
edits the artifact the check reads is Guardrail 9's exact shape, however cosmetic. Queued for the
improvement pass.

### Gate

`python3 dev/tests/phase-ratchet-is-asserted.py --phase 9 --negative-control` → **20/20**, rc 0 —
unchanged across a seventeen-row catalog edit, which is `T11c″-b`'s decoupling holding. The live arm
→ rc 1, **25 of 91 not green, 12 BLOCKING-ALWAYS, property 4 silent**; rc 1 is correct and expected,
and properties 2 and 3 are `P9-T11g`'s and `P9-T11d`'s work.

`python3 dev/tests/spec-ids.py` → rc 0, all 8 arms PASS, **251 check IDs defined in 09** — unchanged,
because nothing was added: seventeen definitions moved from prose into rows.

`dev/tests/invariants-gate.py` **31/31** · `python3 -m unittest discover dev` **OK, 414 tests** ·
full `dev/L0-CHAIN.txt` clean · prettier over the whole `origin/main...HEAD` changed set.

**Resume at `harness-run`, unit `P9-T11g`** — now thirteen required checks with no green results row.
~~Eleven are runs to be made; **V-CMP-011** and **V-CMP-020** are builds, and they are the only two
obligations of this phase with no implementation at all.~~ **Wrong, and `T11g-1` found out how
wrong: one run and twelve builds.** This sentence was written from the ratchet's `hint` column, which
lists files that _name_ a check ID — and the column's own footer says it is unweighted and that a
file naming an ID may be disclaiming it. Nine of the eleven were named only by parser fixtures, by
`binding.md`, or by the skills that talk about the check. See § `P9-T11g-1`. Then `P9-T11d` (the
workload pair at L2, which must start by pointing the fixture at an agent image this repository
actually builds).

---

## P9-T11g-1 — the audit that found twelve builds where the row promised eleven runs · 2026-07-31 · ✅

### What the row said, and what was actually there

`P9-T11g` was scheduled by `T11f` with this text:

> Eleven are **runs to be made, not rows to be written** — asserted by VAP tests, `dev/tests/` lints
> and `invariants-gate.py` arms, and 09 §9.4 makes the record the evidence.

That is a claim about the tree, and it was made without reading it. ORIENT read it instead. For each
of the thirteen the question was the same — _name the artifact that asserts this property_ — and the
answers were:

| Check                           | Asserted by                                                                                                             | Verdict |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------- |
| V-MET-012                       | `dev/tests/spec-ids.py`, two arms, green on the L0 chain since PR #29                                                   | **run** |
| V-CTR-003, V-CMP-011            | nothing                                                                                                                 | build   |
| V-CMP-020                       | nothing                                                                                                                 | build   |
| V-MET-001, V-MET-008, V-MET-009 | nothing — their only hits are `binding.md`, the milestone/improve skills, and ratchet-parser fixtures                   | build   |
| V-CTN-004, V-CTN-012, V-CTN-017 | nothing on either chain. No RBAC-parsing lint exists at L0; the VAP corpora that would cover 012 are **run by nothing** | build   |
| V-CTN-001, V-CTN-015, V-CTN-016 | nothing — no `dev/verify/*-l2.sh` names or asserts any of the three                                                     | build   |

**One run. Twelve builds.** And a fourteenth check nobody had listed: **V-MET-002**, BLOCKING-ALWAYS,
not green, named nowhere in this file. It reaches the required set only through `T11c‴`'s themed
`V-MET-001…009` ratchet row — which is the row working correctly, and is precisely why a themed row
needs a task to answer it.

### Where the wrong premise came from

The ratchet arm prints a `hint: named by …` column, and the eleven-runs sentence is what that column
looks like if you read it as coverage. The column's own footer says not to:

> The `hint` column is UNWEIGHTED and no property reads it. A file naming a check ID may be
> disclaiming it: `pair_netpol_test.go:35` names V-ISO-001/002 to say they are L2 and belong
> elsewhere. Only a results row is evidence (09 §9.4).

Every one of the nine mis-scheduled checks is a case of that. `V-CTN-004`'s hint names
`.claude/harness/verify-phase.workflow.js` (a workflow that would run the check if it existed),
`dev/test_invariants_gate.py` (a fixture string), and
`examples/gitops-repo/policy/tests/vap_actor_negatives.yaml` — a corpus that is real, correct, and
**executed by nothing**: grep finds it referenced only by `LEDGER.md`, this file, and itself.
`V-MET-008`'s hint names `binding.md` and `harness-milestone/SKILL.md`, which are the two documents
that _require_ the check. A check is not implemented by the sentence demanding it ([[LSN-019]]).

### What was built

`dev/tests/crd-has-no-authority-fields.py`, closing **V-CTR-003 and V-CMP-011 with one artifact**.
09 states them separately — §6.5 as _"No authority fields in the CRD schema"_ citing 06 §10, §6.15 as
_"holds none of the prohibited authority field names and sets no `x-kubernetes-preserve-unknown-fields`
on `spec`"_ — but 06 §10 writes the property once, and V-MET-013 forbids two **definition sites** in
09, not two IDs sharing an **implementation**. Two files reading the same YAML would give one
sentence two chances to drift.

Five properties, and three of them exist because the obvious version of this check is a grep that
passes for the wrong reason:

1. **Depth.** `spec.rbac` is the spelling review catches; `spec.security.rbac` is the one that merges.
   The walk covers the whole `spec` subtree, 271 property names deep.
2. **The schema stays closed.** One `x-kubernetes-preserve-unknown-fields` anywhere under `spec` and
   pruning stops — at which point property 1 is asserting that a name is absent from a schema that no
   longer decides what is present. Scoped to the Agent CRD deliberately: `actionrecords.yaml`
   legitimately carries two, for the opaque payload and patch.
3. **Non-vacuity.** The `spec` node must exist, be `type: object`, and carry at least 50 property
   names. Six absent strings pass just as happily against a file the walk failed to read ([[LSN-035]]).
4. **The Go source.** `config/crd/bases` is generated and **no PR check runs `make manifests` and
   diffs the result** — verified, not assumed. So between adding `ScopeOverride string` to a type and
   remembering to regenerate, properties 1–3 read the old bytes and pass. This is the arm with the
   most reach and it is the one a grep-shaped check would not have.
5. **The L2 arm still exists.** This check asserts a shape and cannot observe pruning;
   `webhook-negatives-l2.sh`'s V-9 arm applies `spec.rbac` against a real API server and asserts it
   comes back gone. Deleting the half that proves the mechanism must not be silent.

The reader is a dependency-free structural walk, for the reason `spec-ids.py` and `yamlsubset.py`
each have one: L0 installs nothing. `yamlsubset` itself cannot read a CRD — controller-gen folds Go
doc comments into multi-line plain scalars, outside its accepted subset — and widening a parser two
corpus lints depend on, to read a seventh file, is a change to **their** blast radius, not to this
check's. A line the walk cannot classify raises rather than being skipped, because a walk that
silently stops covering a region still returns a plausible-looking list of keys.

### What moved

|                           | before |  after |
| ------------------------- | -----: | -----: |
| required                  |     91 |     91 |
| green                     |     66 |     69 |
| not green                 |     25 | **22** |
| of those BLOCKING-ALWAYS  |     12 | **11** |
| property 4 (under-naming) |      0 |      0 |

Three closed: V-MET-012 (BLOCKING-ALWAYS, a row it had been owed since PR #29), V-CTR-003 and
V-CMP-011 (built here). The required set did not move, which is correct — nothing was added to the
gate and nothing retargeted out of it.

### Gate

`python3 dev/tests/crd-has-no-authority-fields.py` → rc 0 · `--negative-control` → **6/6**, rc 0.
Two of the six mutations differ only in depth (`properties.rbac` vs
`properties.security.properties.actorServiceAccountName`), because a control that asks only _"did
anything fail"_ cannot tell whether the nested arm executes at all ([[LSN-035]]) — and nested is the
arm that would rot.

`python3 dev/tests/negative-controls-name-their-rule.py` → PASS, and the new control is in its corpus.
`python3 dev/tests/spec-ids.py` → rc 0, 8/8, **251 check IDs unchanged**.
`dev/tests/invariants-gate.py` **31/31** · `python3 -m unittest discover dev` **414 OK** ·
full `dev/L0-CHAIN.txt` clean · prettier over the whole `origin/main...HEAD` changed set.
`phase-ratchet-is-asserted.py --phase 9` → rc 1 (**22 of 91 not green, 11 BLOCKING-ALWAYS**, property
4 silent); rc 1 is correct and expected, and properties 2 and 3 are `T11g-2/3/4`'s and `T11d`'s work.
`--negative-control` → **20/20**, rc 0.

### Resume

**`harness-run`, unit `P9-T11g-2`** — the measurement family: V-MET-001, **V-MET-002**, V-MET-008,
V-MET-009. Four BLOCKING-ALWAYS checks about the check set itself, none implemented, and one tool
over `verification/traceability.yaml` rather than four. Then `T11g-3` (V-CMP-020 and the L0 arms of
V-CTN-004/017), `T11g-4` (the L2 containment arms on `gke-scratch-kube-agents-dev`, including a
runner for the VAP corpora that nothing runs today), then `P9-T11d`.

---

## P9-T11g-2a — the check set had no record of what implements it · 2026-07-31 · ✅

### Why the row split first

`T11g-2` was scheduled as _"one tool over `verification/traceability.yaml`, not four"_. That file
answers a **different question** from the one V-MET-002/008/009 ask.

> **Correction, 2026-07-31, same day, by the next unit's ORIENT.** This section first said
> `verification/traceability.yaml` _"does not exist"_. It does, and has since `P8-T10` (`ead358e`) —
> 71 KB, 177 entries, V-MET-011's artifact and green on every run. The claim came from asking
> whether §8's `R-` mapping existed and reporting the answer against the filename §8 happens to use
> for it. The split's conclusion is unchanged and its reason is now the true one, below.

`traceability.yaml` maps the **177 Verification bullets** of 01–08 to check IDs. 09 §8 asks for a
mapping over **every normative statement** — must / never / always / is rejected / is a defect / may
not, plus every mandated-behaviour table row — which §8.1 counts at **~538**. Those are two
populations differing by a factor of three, and the larger one has never been enumerated: a
repo-wide search for `R-<doc>.<section>-<n>` returns exactly two hits, and both are the sentences in
§8 that _define_ the scheme. `verification/coverage.yaml`, which §8.1 names as the baseline's home,
is genuinely absent.

So §8's chain is **requirement → check → implementation**: V-MET-002/008/009 read the first link and
need the enumeration built first (`T11g-2b`), V-MET-011 already holds a 177-bullet slice of it, and
the last link had no artifact at all. That last link is this unit.

### Why a registry and not a grep

`phase-ratchet-is-asserted.py` already prints a `hint: named by …` column, built from
`git grep <check-id>`, under a footer that disclaims it in as many words: _"The `hint` column is
UNWEIGHTED and no property reads it. A file naming a check ID may be disclaiming it."_

**The disclaimer was earned again this phase, at a cost.** `T11g` was scheduled off that column,
promising _"eleven runs to record and two builds"_. The tree held one run and twelve builds
(§ `P9-T11g-1`). Nine of the eleven were "named by":

- the ratchet's own `--negative-control` fixture (V-ADV-003, V-BRK-001);
- `.claude/harness/binding.md` and the skills that _require_ the check (V-MET-002/008/009);
- `examples/gitops-repo/policy/tests/vap_actor_{positive,negatives}.yaml` — corpora that are real,
  correct, and **executed by nothing on either chain** (V-CTN-012);
- `pair_netpol_test.go:35`, which names V-ISO-001/002 in order to say they belong at L2 and are
  asserted elsewhere.

A grep cannot separate an assertion from a citation from a disclaimer, and a second grep will not
either. `verification/implementations.yaml` is a human's answer, written once; this check is what
stops it drifting from the tree it describes.

### The registry

81 entries. Each is either `runs:` + `asserts_in:`, or a single `unimplemented:` string. Curated,
not generated — the header says so at length, because the first draft _was_ generated by ranking
grep hits and it got V-MET-013 (a prose mention), V-MET-014 (one of 21 compliance declarations
rather than the enforcer), V-ISO-001 (the known disclaimer) and V-CTR-002 (only the V-7 slice)
wrong. Seventeen entries carry a curated override for exactly that reason.

### The check — seven properties

| #   | Property                                                                                                                         | Why it is not redundant                                                                                                    |
| --- | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 0   | The reserved `V-{XXX,QQQ,ZZZ}-*` fixture namespaces are still absent from 09 §6                                                  | Property 6's exit must not rot into a blind spot over a real suite code                                                    |
| 2   | Every registry key is a 09 §6 ID                                                                                                 | A renamed or retired ID leaves a row pointing at nothing, which reads exactly like a row pointing at something             |
| 3   | Every `asserts_in` exists **and names its own check ID**                                                                         | This is the half that makes a later grep honest — and it caught the seven anonymous implementations below                  |
| 4   | Every `runs` is on `dev/L0-CHAIN.txt` / `dev/L2-CHAIN.txt` verbatim (or is one of the two entry points) **and reaches** the file | An implementation nothing runs is not an implementation — the VAP-corpus lesson, mechanized                                |
| 5   | Every check required at this phase **and green** has a row                                                                       | Deliberately not "every required check": a check with no green row is the ratchet's population, and one gap wants one gate |
| 6   | Every `V-XXX-nnn` token in the test corpus is defined in §6                                                                      | 09 §8's second clause, literally — over `dev/tests`, `dev/test_*.py`, `dev/verify` and every `k8s-operator` `*_test.go`    |
| 7   | The `unimplemented:` count may not exceed 1                                                                                      | The 09 §8.1 / V-MET-003 shape: a gate that always fails is a gate someone disables, so the remainder is named and pinned   |

Property 6 has two exits, and **both are counted and printed on the pass line** — an escape hatch
nobody can see is how a check stops checking without ever going red. The reserved namespaces let a
negative control invent IDs that will never be real; the `not-a-check-id` line marker covers the one
case where naming a non-existent ID _is_ the sentence, which
`invariants-gate.py`'s _"there is no V-CTR-021, so it is not a one-letter slip"_ is.

The two entry points that appear on no chain line are `make -C k8s-operator test` (`binding.md`
§Build's Go entry point, absent from L0 because it needs envtest) and
`python3 -m unittest discover dev`. Both are named in the source with the reason.

It imports `parse_catalog`, `parse_ratchet`, `parse_acceptance_table`, `parse_results`, `is_green`
and `latest_phase` from `phase-ratchet-is-asserted.py` rather than re-deriving them: three
definition sites of "required" and "green" are three chances for this check and the phase gate to
disagree (V-MET-013).

### Three findings

1. **V-CTR-001 is green, required at phase 9, and nothing asserts it.** Its `evidence_ref` is
   `go test ./...` over every package — which attests that the suite passes, not that every shipped
   `Agent` CR validates and that apply→get→re-apply is a no-op diff. There is no
   `k8s-operator/config/samples/`, and no test reads the shipped CRs and round-trips them. Recorded
   as the registry's single `unimplemented:` row: **published, not hidden**. Correcting the results
   row or building the check is its own unit.
2. **V-CTN-035 and V-CTN-036 carry a `pass` whose `evidence_ref` is a list of spec citations**
   (`01 §3, 02 §6/§10, 07 P11-T4/P12-T5/Accept (f), 09 rows 485–486 + phase-12 ratchet (ead358e)`),
   not a command. Both are phase-12 checks and sit in the 66-not-required set, so neither is a
   phase-9 halt — but a citation is not evidence (09 §9.4), and the same shape is what
   [[LSN-046]] was opened for. Queued to the improvement pass.
3. **Seven required-and-green checks did not declare their own check ID anywhere executable** —
   V-BRK-023, V-CTR-016, V-CTR-017, V-CTR-018, V-GAT-001, V-MET-014, V-REV-008. Every one of them is
   genuinely asserted; the pairing simply lived in nobody's head. Fixed in place: each now carries
   the declaration in the file that asserts the property, written after reading the test names,
   not inferred from the grep that found the file.

### Gate

`python3 dev/tests/check-ids-have-implementations.py` → rc 0 (81 IDs mapped; 112 distinct check IDs
in the test corpus, all §6-defined; 4 reserved fixture tokens and 4 `not-a-check-id` lines, both
counted on the pass line; 1 `unimplemented:` against a ceiling of 1).
`--negative-control` → **8/8**, rc 0 — and the fixtures are read out of the tree
(`_a_required_green`) rather than listed, so the control cannot go stale against it.

`python3 dev/tests/negative-controls-name-their-rule.py` → PASS, **14** controls, the new one in its
corpus. `python3 dev/tests/spec-ids.py` → rc 0, 251 check IDs unchanged.

`phase-ratchet-is-asserted.py --phase 9` → rc 1 (**21 of 91 not green, 10 BLOCKING-ALWAYS**);
rc 1 is correct and expected — properties 2 and 3 are `T11g-2b/3/4`'s and `T11d`'s work.

### Resume

**`harness-run`, unit `P9-T11g-2b-ii`** — see the section below. Then `T11g-3` (V-CMP-020 and the
L0 arms of V-CTN-004/017), `T11g-4` (the L2 containment arms on `gke-scratch-kube-agents-dev`,
including a runner for the VAP corpora that nothing runs today), then `P9-T11d`.

---

## P9-T11g-2b-0 — the spec-text exemption follows the content · 2026-07-31 · ✅

Not a planned unit. `verification/requirements.yaml` is a verbatim mirror of the normative
sentences in 01–08, and two L0 lints scan every tracked file for bytes that must not appear in
source: the retired `ALLOW_ALL_USERS` hatch (`closed-allowlist.py`, V-CTR-002/V-CTR-014) and any
`kubeagents.` domain the operator does not serve (`api-group-single-sourced.py`, LSN-032). Both
already exempt `docs/design/` — a spec has to be able to describe what was removed and why — and
both fired on the mirror the moment it existed: 07 §2's acceptance text names `*_ALLOW_ALL_USERS`
while describing its deletion, and 06 §8's observability table names eleven OTel attribute keys
(`kubeagents.action_id`, `kubeagents.risk_class`, …) that are not API groups.

**Neither firing was a regression, and that is exactly why it needed its own unit.** Guardrail 9:
a check may not change in the same unit as the work whose failure motivated it. So the corpus
change landed first, alone, at `9137144`, and the enumeration followed.

**The exemption follows the content.** This is not a new principle — `closed-allowlist.py` already
carries `verification/results.csv` for the same reason, added 2026-07-26 when V-CTR-014's evidence
rows moved out of the ledger into a CSV. Both new entries are an **exact path, never a
`verification/` prefix**, and the file says why: that directory will hold per-run manifests written
by whatever produced them, and a prefix would exempt those sight-unseen.

**For the group lint, the path exclusion is the _narrower_ of the two available fixes.** The
obvious alternative was to add the eleven attribute keys to `NON_GROUP_ATTRIBUTES`. That set is
closed on purpose — its comment says a typo in an attribute key (`kubeagents.agnet_name`) is the
same class of silent defect as a typo in a group, so a new attribute should be a conversation — and
widening it would have exempted those eleven spellings in **Go and YAML source too**. Excluding one
generated file keeps the closed set closed.

**Over-breadth was probed, not argued.** A sibling `verification/probe-sibling.yaml` carrying both
an `ALLOW_ALL_USERS` emission and `kubeagents.wrong.io` is still caught by both lints; the probe
file was then removed and both returned to green. The mirror also cannot become a smuggling route
on its own: its `text:` values are asserted equal to the spec's own sentences on every L0 run, so
an emission would have to enter through `docs/design/` first — where it is already exempt, and
where it would be read.

**One gap, recorded rather than closed.** Neither lint has a `--negative-control` entry point of
the shape [[LSN-035]] asks for, so this unit's over-breadth property is proven by a probe in one
session rather than by a committed row that re-runs on every chain. Carried to the phase-9
improvement pass.

---

## P9-T11g-2b-i — the denominator 09 §8 asks for did not exist · 2026-07-31 · ✅

09 §8 is the obligation that makes "comprehensive" provable: _"Every normative requirement in 01–08
maps to at least one check ID."_ That sentence needs a population, and the repo did not have one.
So §8.1's _"roughly 45% of the set is uncovered at baseline"_ was a percentage with no list behind
it — which is the precise shape V-MET-009 exists to forbid.

### Why not `verification/traceability.yaml`

Because it answers a different question, and §8 itself is the source of the confusion: §8 names
`traceability.yaml` as the emission target for requirement → checks, while §14 and V-MET-011 use
that same file for the **177 bullets of the six spec Verification sections**. Those are different
populations — 177 bullets against 853 statements — and the collision is in the spec, not in the
tree. It is an ambiguity rather than a contradiction (one file _could_ hold both), so it is not a
§8.5 halt; it is a decision, recorded as one. A second ID space in that file would give
V-MET-011's green a second thing to mean, so the enumeration went to a new
`verification/requirements.yaml`.

### What counts as a normative requirement

09 §8 defines it: _"any statement using must / never / always / is rejected / is a defect / may
not, and every row of every mandated-behaviour table."_ Mechanised as two rules — a **sentence**
carrying one of those keywords, and **every data row of a mandated-behaviour table**. Fenced code
is skipped (06 alone has 31 keyword-bearing lines inside fences). Sentence splitting protects
inline code, links, `§4.4` references, decimals and abbreviations first, or a naive `.` split
shreds them.

**The "mandated-behaviour" qualifier was kept rather than quietly read as "every table".** Tables
in **01** and **07** are excluded and they are the only exclusions: 01's are an audience map and a
current-vs-intent delta, 07's are the phase task schedules and the standing-deferral register.
Those describe what the _project_ will do, not what the _system_ must do, and no check will ever
cover a row of a delivery schedule. Both exclusions are **counted and printed on the pass line**,
because an escape hatch nobody can see is how a check stops checking without going red. Sentences
in 01 and 07 still count — 07 §5's "tests are replaced, never deleted" is a real obligation, and
09 §8.1 says V-MET-003 is its mechanised form.

The six **Verification** sections are excluded wholesale, as V-MET-011's space, and the section
list is imported from `dev/tests/spec-ids.py` rather than restated.

### Calibration against §8.1 — and why two baselines are recorded

§8.1 records **148** normative requirements for 02. Applying both rules to 02, counting every table
row including its Verification section, gives **147**. The audit is very nearly reproducible there
— and is _not_ uniformly reproducible: **78** for 05 against ~204, and **"96 (groups)"** for 06
against ~374. That audit grouped some documents and enumerated others.

So `verification/coverage.yaml` records **both**: §8.1's published audit, asserted cell for cell
against the spec text so it cannot drift, and this enumeration's own count beside it. A ratchet
needs a baseline in the same units as its measurement, so V-MET-008 will run against `totals`.
Pinning it to §8.1's numbers against a different denominator would not be stricter or weaker — it
would be incoherent.

### The check — six properties

| #   | Property                                                        | Why it is separate                                                                                                       |
| --- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| 1   | The enumeration is current with the specs, compared **by text** | A count comparison passes when a sentence is reworded, and a reworded requirement is one a mapping now points at falsely |
| 2   | IDs are well-formed and contiguous 1..n per section             | A gap means a mapping points at a requirement that is not there                                                          |
| 3   | `coverage.yaml`'s totals agree with `requirements.yaml`         | The published artifact is derived; a stale derivation reads exactly like a current one                                   |
| 4   | The uncovered list is **published by ID, not counted**          | V-MET-009 proper — a percentage with no visible remainder is how the audit stops silently                                |
| 5   | The published list is complete and every ID resolves            | A truncated list reads exactly like a complete one                                                                       |
| 6   | The recorded §8.1 baseline equals 09 §8.1's table               | The baseline is read out of the spec, so it cannot be edited into agreement                                              |

`--negative-control` **8/8**: a spec statement left unenumerated, an enumerated one deleted, one
silently reworded, the uncovered list replaced by its count, the list truncated, a requirement
marked covered with no check behind it, the recorded §8.1 baseline edited away from the spec, and a
section's ordinals made non-contiguous.

### Two properties deliberately left to `-2b-ii`

**`checks:` is curated and empty today.** 09 §8 says each check declares the requirement IDs it
satisfies; that mapping is a human's answer, not an extraction, so `--emit` **merges** and never
clobbers it. With it empty, every one of the 853 requirements publishes as uncovered. **That
overstates the gap, and it is the direction that cannot manufacture a green.**

**V-MET-002 and V-MET-008 are therefore still not green**, correctly. Recording a computed
baseline of zero would have pinned V-MET-008's ratchet at the floor where it can never fall — a
false green on a BLOCKING-ALWAYS check, which is the one outcome this unit had to avoid.

### Gate

Required **91** unchanged · green **70 → 71** · not green **21 → 20** · BLOCKING-ALWAYS not green
**10 → 9**.

Full L0 chain clean. `unittest discover dev` **414 OK**. `negative-controls-name-their-rule.py`
PASS at **15** controls (was 14). `check-ids-have-implementations.py` PASS at **82** registry
entries (was 81). `results.csv` **181 rows**, CRLF intact.

`make -C k8s-operator test` was **not** run and is not owed: this unit touches `dev/`,
`verification/` and `docs/build/` only, no Go tree.

## P9-T11g-2b-ii-1 — ownership, derived from the catalog; and the ratchet that needs no curation

**V-MET-008 is green.** `dev/tests/coverage-ratchet-holds.py` + `verification/coverage-ratchet.yaml`.

**Ownership is derived, not declared.** 09 §8's two coverage gates are complements of one
partition: V-MET-002 demands full coverage of the requirements _owned by_
V-CTN/V-BRK/V-REV/V-ISO/V-ADV, V-MET-008 ratchets the rest. The incentive on a hand-drawn boundary
is not symmetric — calling a section unowned shrinks the BLOCKING-ALWAYS obligation and grows the
lenient one — so the partition is read out of 09 §6's `Source` column, which has been reviewed for
four phases and moves when the catalog moves. 38 sections resolve; **420 of 1030 obligations are
load-bearing-owned, 610 are elsewhere.** Prefix-inclusive and exact section matching agree at 420,
and the pass line prints both so the day they diverge is visible.

**The finding, and why section-level non-emptiness was not enough.** The row above asked for one
hard failure: _a load-bearing suite that derives to zero owned sections_. Building it surfaced a
second, one level down, that the first would have waved through. **V-ISO derives exactly one
section** — `05 §8`, recovered from §6.4's prose because its table has no `Source` column at all —
and `05 §8` is a **Verification section**, which `verification/requirements.yaml` excludes by
design. Its obligations live in the other ID space, `verification/traceability.yaml`'s
`<doc>§<sec>#<n>`, under V-MET-011. So a requirements-only reading gives V-ISO **one section and
zero obligations**: V-MET-002 asks a BLOCKING-ALWAYS suite for nothing at all, and the pass line
reads `V-ISO 1 section`, which is not zero and would not have failed. Both spaces are now read, a
section resolves in whichever space its obligations live in, and non-emptiness is asserted at
**both** granularities. V-ISO's eighteen `05§8#n` bullets are real, curated and already gated by
V-MET-011; what the single-space reading would have destroyed is this check's ability to say so, and
its ability to tell that case apart from a suite that owns nothing anywhere.

**Arrival is keyed on text, not on IDs.** `R-<doc>.<section>-<n>` is positional, so inserting one
`must` at the top of a section renumbers every statement below it — an ID-keyed arrival gate would
fire on all of them and name the wrong sentence in every case. `baseline.digests` holds 853
whitespace-normalised content digests instead: reordering changes nothing, an insertion adds exactly
one, a rewording swaps one for one and reads as an arrival, which is the honest reading. The three
exits are §8.1's own — map it to a check, name a deferral in V-MET-006's shape (blocker, owner,
promotion), or `--rebaseline YYYY-MM-DD`, which is a visible diff. **`--rebaseline` refuses to lower
a per-document floor**, so it can retire an obligation to _cover_ a statement but never retire
coverage already achieved; property 5 asserts the floor from the other side on every run.

**What is deliberately _not_ asserted here.** The floor and the arrival clause are scoped to
`requirements.yaml`. A new Verification bullet is already an arrival gate — V-MET-011 fails the
build on any bullet resolving to no check, in both directions — and a second gate over the same 177
keys would add a second place to re-baseline, not a property. Recorded in the ledger's decisions
table.

**Verification.** `V-MET-008` **pass**, L0, tree. Negative control **11/11**: an arrival with no
check and no deferral · a deferral missing its owner · V-ISO's prose source deleted (zero sections)
· `traceability.yaml`'s `05§8` bullets deleted (one section, zero obligations — the same false green
one level down) · a `Source` cell blanked · a recorded ownership row deleted · the load-bearing
count edited · V-MET-002's worklist truncated · the floor raised above the coverage under it · the
enumerator returning almost nothing · the arrival baseline emptied.

Required **91** unchanged · green **71 → 72** · not green **20 → 19** · BLOCKING-ALWAYS not green
**9 → 8**.

Full L0 chain **0 failures**. `unittest discover dev` **414 OK**.
`check-ids-have-implementations.py` PASS at **83** registry entries (was 82). `results.csv` **182
rows**, CRLF intact.

**The floor is 0 by construction, and this unit does not pretend otherwise.** V-MET-008 passes on
the ratchet, the arrival clause and the ownership derivation — not on coverage. The 357
requirements-space obligations the load-bearing suites own are published **by ID** in
`load_bearing_uncovered:` as V-MET-002's worklist. V-MET-002 remains not green.

`make -C k8s-operator test` was **not** run and is not owed: `dev/`, `verification/` and
`docs/build/` only, no Go tree.

### Resume

**`harness-run`, unit `P9-T11g-2b-ii-2`** — curate `checks:` in `verification/requirements.yaml` for
the **357** load-bearing-owned requirements, then build **V-MET-002** (BLOCKING-ALWAYS). The
worklist is already published by ID; read it out of `verification/coverage-ratchet.yaml` rather than
re-deriving it. Constraints inherited and worth not rediscovering:

1. **Section-citation is not coverage.** A requirement is _owned_ precisely because some
   load-bearing check cites its section, so reusing that citation as the mapping makes V-MET-002
   green by construction — the false green §8.1 distinguishes _fully covered_ from _partial_ to
   avoid. The mapping is per statement.
2. **A requirement with no honest check is left unmapped and V-MET-002 stays red.** §8.1 dates the
   load-bearing draw-down to _"before Phase 10 grants the first write credential"_; buying a green
   with a citation is what that dating exists to prevent.
3. **Ownership and the ratchet are done.** Do not re-derive either, and do not edit
   `coverage-ratchet.yaml` by hand — the ownership half is recomputed and compared on every L0 run,
   so a hand edit only produces a finding. As `checks:` fills in, re-run `--emit` to shrink the
   published worklist; the floor rises with it and `--rebaseline` cannot lower it.

Then `T11g-3` (V-CMP-020 and the L0 arms of V-CTN-004/017), `T11g-4` (the L2 containment arms on
`gke-scratch-kube-agents-dev`, including a runner for the VAP corpora that nothing runs today),
then `P9-T11d`.

---

## P9-T11g-2b-ii-2c-ii-c — the draw-down closes, and a control breaks on its own success · 2026-07-31 · ✅

### The `-c-1` / `-c-2` split was planned and is recorded VOID

The unit was sized as two commits — `-c-1` the three remaining mappings, `-c-2` the green and the
arm move. It landed as **one**, and the reason is worth writing down rather than quietly dropping:

- **V-MET-002's greenness is _caused by_ `-c-1`'s mappings.** There is no tree in which `-c-1` is
  committed and V-MET-002 is still red, so `-c-2` has no independent precondition to establish.
- The two halves **share `dev/L0-CHAIN.txt` and `verification/implementations.yaml`**, and splitting
  a shared file across two commits needs interactive hunk staging, which this environment forbids.

A split that cannot produce two distinct verifiable trees is not a split. Recorded here so the next
reader does not go looking for a `-c-1` commit.

### The three closures, and the one that was nearly wrong

| Requirement  | Row           | Source  | Lvl | Phase  | What it asserts                                                           |
| ------------ | ------------- | ------- | --- | ------ | ------------------------------------------------------------------------- |
| `R-06.2.3-6` | **V-CTN-039** | 06 §2.3 | L0  | 9      | the developer-team tier has **no cloud actor identity**                   |
| `R-03.4.3-8` | **V-CTN-040** | 03 §4.3 | L2  | **10** | no write to an `Agent` CR whose identity is an **ancestor** of the writer |
| `R-03.4.3-9` | **V-CTN-041** | 03 §4.3 | L2  | **10** | actor writes stay inside the **live** tier template                       |

**V-CTN-039 has a clause that is easy to drop and load-bearing.** "No cloud actor" must stay
distinguishable from "no actor at all", so the row also requires the tier's **Kubernetes** actor
grant to still exist. A check that only asserted absence would go green the day the developer-team
actor was deleted outright — the greenest edit of all.

**V-CTN-040 and V-CTN-041 are dated phase 10, and that is a legitimate closure.** The decision hung
on `load-bearing-coverage-is-full.py`'s own docstring, which says it does not ask whether the mapped
check is green, and that _"section citation is not coverage, and a requirement with no honest check
is left unmapped rather than closed with the nearest-looking catalog row."_ **V-CTN-012 was sitting
right there** for the live-tier-template obligation — an existing, green, phase-8 row about
attenuation against a tier template — and taking it would have been precisely the nearest-looking
catalog row. Both obligations are about the **write** side, which does not exist until Phase 10.

Verified against the phase ratchet rather than assumed: `phase-ratchet-is-asserted.py` partitions
V-CTN into `(read-side)` at phase 8 and `(write-side)` at phase 10, and both new rows landed in the
_"68 suite members NOT required here"_ note. **A new V-CTN row dated phase 10 does not enter the
phase-9 required set.**

### The coordinated move, as one tree state

Four edits that had to be simultaneous, because any ordering leaves the arm uncovered for a commit:

1. `dev/L0-CHAIN.txt` — the live arm `python3 dev/tests/load-bearing-coverage-is-full.py` goes **on**
   the chain, above the `--negative-control` line that was already there.
2. `verification/implementations.yaml` — V-MET-002's row, `runs:` matching the chain line verbatim.
3. `dev/tests/invariants-gate.py` — `check_phase_gate_publishes_the_coverage_remainder` **retired**
   with `dev/L0-CHAIN.txt` named as its replacement (invariants §8: a retirement with a named
   replacement, not a deletion). 32 checks → 31.
4. `dev/test_invariants_gate.py` — its 8 tests deleted, 4 entries added to
   `dev/assertion-baseline.json`'s `retired:`, baseline wound 1397 → 1393.

**`dev/verify/verify-phase9.sh`'s section K was KEPT deliberately.** Section J reports _whether_
V-MET-002 is green from the results file; section K is what says **which** obligations are uncovered,
by ID and in their own words. 09 §8.1's draw-down does not end at Phase 9 — it ends at every phase
that adds an owned section, so an empty worklist today is not a reason to delete the thing that
prints tomorrow's.

### The finding: a mutation keyed to a position, not a property

Emptying the worklist turned one of V-MET-008's **own** control mutations into a `MISS`:

```
MISS    V-MET-002's worklist is truncated
         expected a finding containing "is not V-MET-002's worklist"; got ["a new normative
         statement arrived uncovered: R-02.2-4 is not in the arrival baseline recorded 2026-07-31…"]
```

`_mutate(base, 0, lambda t: "\n".join(t.splitlines()[:-30]) + "\n")` is a **positional** chop of
`coverage-ratchet.yaml`'s tail. The worklist was the tail only while it was non-empty; with it empty
the chop landed thirty lines into the arrival-baseline digests, a **different** arm fired, and the
row reported _"the check let a defect through"_ over a defect that was never applied.

**Guardrail 9 was weighed and does not bite.** The _live_ V-MET-008 arm was green throughout, so
re-aiming was never the smallest diff to green for an implementation — what broke is a control whose
target ceased to exist **because the unit succeeded**. Re-aimed rather than deleted, and aimed the
other way: with the computed set empty the two artifacts can only disagree by the **file** publishing
an ID the computation does not, so the mutation now injects one. It also survives the worklist coming
back, where a positional chop only worked at one length.

The adjacent _"the floor is raised"_ mutation in the same file already carried the precedent verbatim
— _"a mutation keyed to `covered: 0` stops applying the day curation moves the floor off zero, and a
no-op mutation scores MISS … over a defect that was never applied ([[LSN-063]])"_ — which is why the
**class** goes to the improvement pass and not just the instance.

### A second finding, second sighting

Deleting 8 tests turned the assertion ratchet red and it named only **4**: the other four have
duplicate names in other classes of the same file, so the `file::name` set collapses them. Already in
the improvement queue; this is its second firing.

### Property 4 of the phase ratchet, closed

Five phase-9 IDs were required by 09 §10 and named **nowhere** in this file's acceptance table —
**V-CTN-038**, **V-CTN-039**, V-CTR-021, V-GAT-024, **V-REV-012**, all added to 09 §6 by this phase's
own coverage draw-down. A `_(ratchet only)_` row now names them. The required set moved **91 → 96**,
and 96 = 95 (09 §10) ∪ 96 (the table).

### Gate

| Check     | Level | Result   | Evidence                                                                                 |
| --------- | ----- | -------- | ---------------------------------------------------------------------------------------- |
| V-MET-002 | L0    | **pass** | 420/420 owned obligations mapped; control 8/8; live arm now on `dev/L0-CHAIN.txt`        |
| V-MET-008 | L0    | **pass** | coverage 357 against a floor of 349; worklist 0; control **11/11** after the re-aim      |
| V-MET-001 | L0    | **pass** | 89 check IDs; 6 lines marked `not-a-check-id`; 1 `unimplemented:` against a ceiling of 1 |
| V-MET-009 | L0    | **pass** | 853 enumerated, **357** covered (was 354), 496 published by ID                           |
| V-MET-003 | L0    | **pass** | **31/31** invariant checks; baseline 143 files / **1393** named tests, 4 retired         |

Full `dev/L0-CHAIN.txt`: **0 failures**, no `VACUOUS:`. Worklist **3 → 0**. Coverage **354 → 357**.

### Resume — and four surveys that are already paid for

The next four units were surveyed in parallel with this one. **Their conclusions are recorded here
so nothing re-derives them**; several overturn what the task rows assume.

**`P9-T11g-3` — the three L0 arms are already authored and sitting untracked in `dev/tests/`:**

- `devteam-has-no-cloud-actor.py` (V-CTN-039) — 5 properties, control **15/15**. Excludes
  `dev/tests/` from its own corpus, because a check about a forbidden identifier necessarily contains
  it. Deliberately stays **silent** on the `kubeagents-devteam-<ns>-gsa` vs
  `kubeagents-developer-team-gsa` drift — that is V-CTN-030's row at phase 11.
- `reader-holds-only-read-verbs.py` (V-CTN-004 L0) — 7 properties, control **16/16**. The allow-list
  `{get,list,watch}` **is** the property; `impersonate`/`escalate`/`bind` appear nowhere in the file.
  **One decision to review:** wildcard `resources: ["*"]` is a finding only when the apiGroups axis is
  also unbounded, because all nine reader roles carry it, 06 §2.2's platform read template requires
  it, and V-CTN-004's own VAP fixture pins it as `EXPECT: ADMITTED`.
- `controller-mints-no-rbac.py` (V-CTN-017 L0) — 9 properties, control **21/21**. Scans the rendered
  ClusterRoles **and** the `+kubebuilder:rbac` markers **and** asserts they agree triple-for-triple
  (110 each side today) — rendered-only misses a marker added without `make manifests`, markers-only
  misses a hand-edit to the file `kustomize build` installs. **Correction to the task row:** the
  controller does **not** create ServiceAccounts; the marker is `get;list;watch`.

Still to do for each: two `dev/L0-CHAIN.txt` lines, an `implementations.yaml` row, a results row.

**`V-CMP-020` is RED with 37 real findings and needs an orchestrator ruling.**
`tier-skills-match-the-allocation.py` is authored and its control is 18/18, but the tree does not
satisfy it: the Developer Team has **none** of the seven workload skills 02 §2.1 assigns it, and the
Platform Agent carries the whole superset. 09 §6 dates V-CMP-020 at **phase 9**; the fix is
`07 P13-T5`, at **phase 13**. V-CMP is BLOCKING-PHASE, not BLOCKING-ALWAYS, so a `deferred` with a
named blocker is available — but the row's _phase_ is the thing that has to move, or the fix does.

**`P9-T11g-4` — three of the six L2 arms already exist and claim no ID.** This is the same shape
`-2c-ii-b-1` paid for: _an obligation with no `checks:` entry is a catalog fact, not evidence the
property is unbuilt._

- **V-CTN-015 and V-CTN-016** are asserted **today**, on the chain, by
  `dev/verify/webhook-negatives-l2.sh` (`dev/L2-CHAIN.txt:114`) as rules **V-5** and **V-4** — each
  via `reject()`, which fails both on admission and on a rejection whose message omits the expected
  field path (`spec.scope`, `metadata.namespace`). The script names only `V-CTR-002`. What is missing
  is the **`¬` half**: every existing arm is a rejection, so the absent control is the _positive_ (a
  second Agent in a **different** scope admitted; a developer-team Agent in its own namespace
  admitted). Section 12 already has per-tier positives to build on.
- **V-CTN-012** is asserted by `dev/tests/negative-attenuation.sh`, reachable from the chain
  transitively through `verify-phase7.sh`. **Trap:** the catalog names `vap-agent-scope` as the
  denying policy and **that policy does not exist**, in the tree or on the cluster — it arrives at
  `P10-T1`. The live cluster carries `kube-agents-agent-readonly` (3 validations). Say in the header
  which policy the arm measures; do not let the two names silently differ.
- **V-CTN-017's L2 arm** (parse the live `kubeagents-manager-role`) is the only one genuinely
  unbuilt.

**The VAP corpora are exercised, but only to "at least one".** `examples/gitops-repo/policy/tests/`
holds `vap_actor_positive.yaml` (5 docs, all EXPECT ADMITTED) and `vap_actor_negatives.yaml` (7 docs,
all EXPECT DENIED). Both headers name `tests/e2e/vap_negative_test.sh`, **which does not exist**. What
_does_ touch them is `gitops-tree-applies-l2.sh`, which runs **one** `apply --dry-run=server` over the
whole multi-doc file and requires one denial anywhere in it — so docs 2–7 could all be admitted and
the line stays green. The runner to build splits on `^---$` and asserts **per document**, with the
needles the corpus header already specifies (`"broker-operations grant"` for negatives 1–6,
`"agent RBAC may grant"` for negative 7), the doc counts pinned at 5 and 7, and the whole section
gated behind `p2_assert_policy_live` — an ADMIT arm reads identically against an absent policy
([[LSN-006]]).

**Six phase-9-required checks are owned by no task row.** Triaged:

| Check                                 | State                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **V-CMP-006**                         | **green already, no results row.** `P9-T10` shipped fix and check in `9939ffe` and wrote no row. `dev/test_mcp_env_declared.py` 6/6. Cheapest item in the phase. Rule on the L2 arm (catalog says L0+L2) or defer it beside the standing L3 deferral.                                                                                                                                                              |
| **V-BRK-005**                         | **green already, no results row — and the only BLOCKING-ALWAYS of the six.** `broker-refuse-l2.sh` arms **B** (journal-gone → HTTP 503 `journal-unavailable`, zero ActionRecords) and **C** (restored → HTTP 202) _are_ the assertion and its `¬` control; they ran 17/17 rc 0 on 2026-07-31 and were recorded under **V-ISO-006**. Bind the ID, re-run, record.                                                   |
| **V-GAT-002**                         | **unbuilt, but cheapest of the three.** `undo-coverage-l2.sh` already carries the corpus's `expectClass` into the transcript and says in its own comment it is _"NOT an assertion"_. Add a per-class spot-check comparing it to `spec.classification.class`. Same commit: delete the dangling pointer to `dev/verify/classifier-corpus.sh` under suite prefix `V-CLS` — **neither the file nor the suite exists**. |
| **V-CTR-007 + V-RUN-007 + V-RUN-008** | **unbuilt; ONE unit, a new `dev/verify/brake-l2.sh`.** They share every fixture: a deployed pair, a paused `Agent`, a `FleetFreeze`, the controller scaled to 0, and an RBAC revoke/restore cycle `broker-refuse-l2.sh` arms B/C already implement. Splitting them stands the same fixture up three times. Size `large`.                                                                                           |

**Two lost hand-offs, both worth carrying to the next ORIENT.** `verification/results.csv:71` routed
**seven** IDs to _"verify-phase9.sh in P9-T9"_ and nothing ever picked them up — T9b split five ways
and no descendant claimed any. A "routed to a future task" note in results.csv **is not a task**, and
this is the second time in the phase a hand-off recorded in prose was lost (the first was the `hint`
column `P9-T11g-1` audited).

**`P9-T11d` — the blocker is confirmed and larger than the row says.** There is **no agent image and
no broker image in Artifact Registry for `de0486b`**; the newest are `dev-9cb7465-dirty-*` and
`dev-cdad3b1-dirty-*`. `make docker-build*` exits 2 on this host, so the route is
`bash dev/cluster/reload-images.sh all gke-scratch-kube-agents-dev` (Cloud Build, deploy-by-digest,
and it sets `KUBEAGENTS_BROKER_IMAGE` on the **controller** — the CR cannot name the broker image by
design). `multi-agent-namespace-l2.sh:60-98` is the working precedent for resolving a tier image and
`DEFERRED:`-ing rather than failing when it is absent. **And a second, independent blocker:**
`kubeagents-router` is in **CrashLoopBackOff, 137 restarts**, on `missing required --project-id /
KAGE_PROJECT_ID` — a Deployment env/arg problem a rebuild will not fix, and the C15 "one fleet-wide
Socket Mode connection" arm cannot run until it is set. Decide up front whether that fix is in the
unit's diff or a named deferral. **Note also** that V-RUN-001/002/004/009 are pure _shape_ claims and
inherit `multi-agent-namespace-l2.sh`'s licence to use _a_ pullable image, but **V-RUN-005 is a claim
about the agent binary's `wait-for-broker` behaviour** and must pin the digest it actually ran.

---

## `P9-T11g-3` — three checks that assert an absence, and a fourth committed red on purpose

**Landed 2026-07-31.** Required set 96; **green 77 → 80**, not green 19 → 16, BLOCKING-ALWAYS not
green **8 → 6**. Implementations **89 → 92**. `dev/L0-CHAIN.txt` **60 → 66** executable lines — three
live arms and three `--negative-control` arms, each preceded by a `# --- title ---` block naming the
failure mode it exists for.

| Check     | Level | Verdict      | Control | Evidence                                        |
| --------- | ----- | ------------ | ------- | ----------------------------------------------- |
| V-CTN-039 | L0    | **pass**     | 15/15   | `dev/tests/devteam-has-no-cloud-actor.py`       |
| V-CTN-004 | L0    | **pass**     | 16/16   | `dev/tests/reader-holds-only-read-verbs.py`     |
| V-CTN-017 | L0    | **pass**     | 21/21   | `dev/tests/controller-mints-no-rbac.py`         |
| V-CMP-020 | L0    | **deferred** | 18/18   | `dev/tests/tier-skills-match-the-allocation.py` |
| V-MET-001 | L0    | **pass**     | 8/8     | 92 IDs, 6 marked lines, ceiling 1               |
| V-MET-003 | L0    | **pass**     | —       | 31/31 invariant checks, chain 0 failures        |

The three V-CTN arms all keep an open **L2** half, which `P9-T11g-4` owns. Their rows are recorded at
level `L0` and claim nothing about L2 — the ratchet counts a check green when it has any `pass` row
with an `evidence_ref`, so the discipline that keeps that honest is the `level` column, not the
verdict.

### V-CTN-039 — the only row that asserts a principal does not exist

Every other containment check in the catalog asserts what a **named** principal cannot do, and you
can only sweep the verbs of an identity you can name. 06 §2.3's six-row Cloud IAM table gives the
developer-team **actor** no GSA, and this check asserts the tree agrees: none of 8 actor-labelled
ServiceAccounts (of 20) carries a Workload Identity annotation or a placeholder that renders one;
none of 19 tier-classified GSA identifiers (of 45 swept) is `(developer-team, actor)`;
`DEVELOPER_TEAM_ROLES` holds 3 roles and all three are read-only.

Two arms on the annotation, because only one of the two **looks** like the annotation. The literal
`iam.gke.io/gcp-service-account` key is the obvious one. The other is `${AGENT_READER_ANNOTATIONS}`
moved onto the actor's ServiceAccount — a placeholder that renders the same binding at provision
time, in a diff where the key itself never appears.

The last property is what stops the check being satisfiable **by deletion**: the tier's _Kubernetes_
actor grant must still exist. Without it, "the developer team has no cloud actor" and "the developer
team has no actor at all" produce the same green, and the second one is a regression the check was
built to notice.

One control row is `ignored` on purpose. The developer-team **reader**'s GSA name drifts —
`kubeagents-devteam-<ns>-gsa` in one place against `kubeagents-developer-team-gsa` in another — and
that is **V-CTN-030**'s, at phase 11. This check asserts it stays **silent** on it, which is a
property in its own right: a containment check that fires on its neighbour's defect is a check whose
green means less than it appears to.

### V-CTN-004 — an allow-list, because a deny-list already failed once

`READ_VERBS = ("get", "list", "watch")` is the only verb list in the file, and `impersonate`,
`escalate` and `bind` appear **nowhere** in it. That is deliberate and it is the mechanization of 09
§11.4: the phase-0 finding was that a deny-list admitted `impersonate` because nobody thought to deny
it. A deny-list is a list of the attacks you have already imagined.

Nine reader `Role`/`ClusterRole`s across 17 install-path files, 10 rules, every verb inside the
allow-list; no wildcard verb or apiGroup; no `aggregationRule`; 15 actor roles classified the other
way over the same corpus; 23 agent-labelled RBAC files all accounted for.

**"Universally" is bought by property 5, not by the verb sweep.** Every roleRef bound to a reader
subject must resolve to a reader role. One word added to a `subjects:` list makes the reader SA a
subject of the broker-operations binding, which holds `create` on the journal — and **no reader role
changes**. A check that only sweeps the roles reads that diff as clean.

One argued asymmetry, recorded rather than silently taken: a wildcard `resources: ["*"]` is a finding
only when `apiGroups` is **also** unbounded. All nine reader roles carry it, 06 §2.2's read template
requires it, and V-CTN-004's own VAP fixture (`vap_actor_positive.yaml` DOC 4) pins it
`EXPECT: ADMITTED`. Flagging it would put the check in direct contradiction with the policy corpus
it shares a check ID with.

### V-CTN-017 — parse both artifacts, then assert they agree

08 §7 says to assert this by **parsing** the generated RBAC rather than reading it. This parses both
sides and then asserts they agree **triple for triple**: 4 control-plane roles under
`k8s-operator/config/`, 22 rules, 141 `(group, resource, verb)` grants, against 28 `+kubebuilder:rbac`
markers — **110 each side** on the `manager-role` comparison. No verb on an RBAC object, no write on
an identity resource, no wildcard in any position, no `escalate`/`bind`/`impersonate`, all 5 roleRefs
resolve, and no shipped Go file imports the RBAC API.

Either artifact alone has a blind spot the other covers. Markers-only misses a hand-edit to the file
`kustomize build` actually installs. Rendered-only misses a marker added without `make manifests` —
where the grant lands later, in a commit that looks like formatting.

Two rulings inside it. **Read verbs on RBAC objects are not permitted**, and that is 08's call: its
"beyond `get;list;watch`" clause attaches to ServiceAccounts alone, not to RBAC. And
`serviceaccounts/token` is forbidden **separately** from `serviceaccounts`, because TokenRequest
returns the credential rather than the identity — a controller that may read a ServiceAccount and
also mint tokens for it has the thing the read verb was supposed to be safe from.

**Correction to this row's own premise:** the controller does **not** create ServiceAccounts. The
marker is `serviceaccounts,verbs=get;list;watch`.

### V-CMP-020 — `deferred`, committed, red, and off the chain

`dev/tests/tier-skills-match-the-allocation.py` exits 1 on the tree with **37 findings**, and its
negative control is **18/18** from both ends of the drift. Every one of the 37 is the persona
conversion that 02 §2.1's own table — _"Renames the conversion must perform"_ — already describes:
`submit-suggestion` → `apply-change`, `raise-escalation` → `escalate`, `propose-*` → `provision-*`,
the new `delegate`. 02 §2.1 dates it in as many words: _"The old skills exist today under
`agents/*/skills/`; [07](../design/07-implementation-roadmap.md) sequences the swap."_

So this is not a defect the check found. It is a conversion the check will **verify**, and the honest
state until then is `deferred` with a named blocker — not `pass`, and not a chain line that turns CI
red for four phases over work nobody has been asked to do yet. V-CMP is BLOCKING-PHASE, not
BLOCKING-ALWAYS, so 09 §9.6 permits the deferral.

```
Blocker:              07 P13-T5 (persona conversion)
Owner:                phase 13
Promotion condition:  `python3 dev/tests/tier-skills-match-the-allocation.py` exits 0
```

Committing the check **ahead** of its day is the point, and the file carries a header block saying so:
it makes the promotion condition **a command rather than a paragraph**. It gets no
`verification/implementations.yaml` row, which is the registry rule working exactly as written — _"a
check with no green row is absent, and that is correct… do not add a row for work you intend to do."_
Its `runs:` value would not have appeared on a chain, and V-MET-001 would have refused it.

**Two corrections to the recon that produced it**, both from reading the source rather than the
summary. 09 §6 dates this row at phase **8**, not 9. And 07 P13-T5's premise is partly stale: it says
the Developer Team has _"none of the seven workload skills"_, but the tree already gives it
`gke-networking-edge`, `gke-observability`, `gke-reliability` and `gke-storage`.

### The cataloguing finding — recorded, not acted on

09 §6.15 line 701 dates **V-CMP-020** at phase **8**. Line 702 dates **V-CMP-021** — the structurally
identical row over jobs and SOPs, blocked by the **same** persona conversion, cited to the **same**
§5.3 — at phase **13**. One of the two datings is wrong, and it is almost certainly V-CMP-020's.

Correcting the phase cell would remove this row from the phase-8/9 required set, which is exactly the
spec change **Guardrail 9** forbids inside the unit whose check it would green. Routed to
`harness-improve` with both line numbers so the improvement pass does not have to rediscover the pair.

Note the shape: this is the **third** time in phase 9 that a row's **phase**, not its assertion, was
the thing that was wrong.

### Resume

**`harness-run`, unit `P9-T11g-4`** — the L2 containment arms. Sixteen required checks remain not
green, **five** of them BLOCKING-ALWAYS: **V-BRK-005**, **V-CTN-001**, **V-CTN-012**, **V-CTN-015**,
**V-CTN-016**. A BLOCKING-ALWAYS check may not be deferred at all (09 §9.6), so those five are the
critical path to closing the phase — the other eleven (V-CMP-006, V-CMP-020, V-CTR-007, V-GAT-002,
V-RUN-001/002/004/005/007/008/009) are BLOCKING-PHASE and a named deferral is available where it is
honest.
