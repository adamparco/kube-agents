# Phase 4 — Coordination & knowledge (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 4 — Coordination & knowledge".
**Goal:** turn on **indirect coordination** (GitOps + OKF; no vector store in v1) and **push-first
proactivity**. Three deltas land: (1) wire **OKF read/update** into all three tiers (06 §5) so an agent
can retrieve a runbook and a lower tier can raise an escalation a parent picks up — **never** a direct
agent-to-agent call (invariant 3); (2) wire **push-first event triggers** (04 §4) — per-tier Kubernetes
watches on the agent's own read-only SA, plus alert + GitHub webhooks over Pub/Sub, with the heartbeat
demoted to a backstop; (3) define **per-tier heartbeat SOPs** (Cluster Admin + Developer Team scoped
audits) **including the Platform Agent's drift-detection SOP that opens a corrective PR unprompted** (SC4,
01 §7). Every resulting change still flows only through a reviewed PR actuated by CI/CD; agents stay
read-only, and a trigger changes only _when_ an agent wakes, never _what_ it may do (04 §4).

**Phase acceptance (07 §2 "Accept") — decomposed a–e:**

- **(a)** A **Kubernetes watch fires an agent reaction** (e.g. a crash-looping workload) **without**
  waiting for the next heartbeat poll — and it fires **scoped to the tier** (a namespace watch for
  developer-team, cluster-wide for cluster-admin/platform), delivered to the agent's local session.
- **(b)** An **escalation written by a lower tier** (a `knowledge/escalation/<slug>.md` PR) is **picked
  up by its parent** on the parent's next proactive sweep — via shared GitOps/OKF state, **never** a
  direct agent-to-agent call (invariant 3, 03 §11).
- **(c)** An agent **retrieves a runbook via OKF** — reads `knowledge/` for operational context through a
  read-only path that can never become a write path.
- **(d)** **Per-tier heartbeats run scoped audits** — Cluster Admin over its cluster, Developer Team over
  its namespace only; anything a sweep wants to change goes through the propose→review→reconcile loop
  (04 §9), never a direct mutation.
- **(e)** **Inject drift** (RBAC / NetworkPolicy / version skew) → the **Platform Agent detects it and
  opens a corrective PR unprompted** — never a direct fix (SC4, 01 §7; corrective/revert-PR path 04 §5.1).

**Touched Verification suites:** **04 §9** (only-write-path-is-a-merged-PR; heartbeat-via-the-loop with
no direct-write audit events; reconcile-failure → corrective PR), **06 §10** (OKF validator: every
`knowledge/` file carries a valid `type` + links resolve), **03 §11** (the load-bearing negatives —
read-only SA still holds when driven by an event trigger; **invariant-3 no-direct-call** proven by the
escalation round-trip going only through GitOps; egress default-deny with the added Pub/Sub-subscriber
allow), plus **08 §7** (pre-created identity; controller mints no RBAC — regress) and **05 §8** (chaos —
regress). **Load-bearing subset active this phase: 03 §11** — specifically the read-only ceiling under
push triggers and the **no-direct-agent-call invariant (3)**.

**Source of the breakdown:** a survey of the Phase-4 component surface (the `k8s-event-watcher` sidecar,
the three per-tier `session_kv_server.py` inject seams, `submit_suggestion.py`, `local-dev/okf-validate.py`,
and the per-tier `cron/` + `governance/` SOP trees) fed a **design judge-panel workflow** (3 architects ×
3 adversarial judges + synthesis) that resolved the five load-bearing decisions **D1–D5** below.
**Verdict: `verifiability-first` won unanimously across all three judges** (correctness 9/8/9,
invariant-safety 8/8/9, verifiability 9/9/9, zero fatal flaws) because it is the only design that gives
every acceptance bullet a **hermetic Kind assertion** while naming and fixing the two real latent defects.
`invariant-first` was the close runner-up (safest by construction); the synthesis **grafts its two cheap
wins**: the **fail-closed watcher `validate()`** (rejects a namespace-scoped tier attempting a cluster-wide
/ RBAC watch before it can crash-loop or over-reach) and the **sparse `knowledge/`-only read checkout** (a
read can never accumulate into a commit). The panel also surfaced **two load-bearing pre-conditions the
push work sits on** — the inject seam is bound `0.0.0.0:8699` with **no auth**, and `inject_message` is
hardcoded to k8s-event fields — both fixed **first** (P4-T1/T2) before any new push source is wired.

---

## Architecture decisions (load-bearing — resolved before breakdown)

### Track S — Inject-seam pre-conditions (fix before adding push sources)

**S1 — The session-inject seam must be authenticated + loopback before it carries more sources.** Today
`deploy/shared/docker-entrypoint.sh:44` starts uvicorn on `--host 0.0.0.0 --port 8699` and
`session_kv_server.py` checks **no** bearer / `X-Asserted-Caller` on `POST /sessions` (:92) or
`POST /sessions/{id}/inject` (:328) — the `k8s-event-watcher` **sends** an `Authorization: Bearer` +
`X-Asserted-Caller` (injector.go:85-134) that the server **ignores**. Phase 4 funnels _more_ push sources
(alerts, GitHub) onto this seam, so it must first be **bound to `127.0.0.1`** (all in-pod callers — the
watcher sidecar and the eventingress relay — are same-pod) **and** enforce the bearer + a
`proxy_identities`/owner check server-side (the `--owner=<tier>` claim is otherwise unenforced). Agents
stay read-only so no write-path invariant breaks, but an unauthenticated non-loopback wake vector is a
prompt-injection + attribution (invariant 5) gap. Lands in **all three** per-tier copies
(`agents/{platform,cluster-admin,developer-team}/scripts/session_kv_server.py` + the shared entrypoint).

**S2 — `inject_message` must branch on a `kind` discriminator.** `inject_message` (:328) extracts
k8s-Event fields (`reason`, `namespace`, `kind_of_object`) and formats a Google-Chat CrashLoop-style
card; it **ignores** any `kind` field. Alert / GitHub / escalation-notice payloads routed through it are
mangled or mislabeled. Add a required `kind` discriminator (`k8s-event` | `alert` | `github` |
`escalation`) with a per-kind render branch; unknown kind → 400 (fail closed). Lands in all three copies.

### Track A — Push-first event triggers (04 §4)

**D1 — One in-pod delivery contract: every machine push terminates at the local session-inject seam.**
All non-chat push (K8s watches, alerts, GitHub webhooks) delivers via `POST {daemon}/sessions` then
`POST /sessions/{sid}/inject` on the **local** daemon (`127.0.0.1:8699` after S1). The **ChatOps router
is left untouched** — human chat keeps its `AllowedUsers` authz gate (receiver.go:90 / authorize.go fail
closed on a sender-less event); machine events must **not** reuse the human authz path. In-cluster K8s
watches keep delivering via the in-pod `k8s-event-watcher` sidecar (D2). The genuinely-cloud legs (alert
Pub/Sub transport, GitHub webhook HMAC) are isolated behind **one deferrable `eventingress` component**
(subscribe-only Pub/Sub, never publisher); on Kind that component is **replaced by a direct synthetic
`POST` of a `{kind:alert|github}` payload to `127.0.0.1:8699/inject`**, so the in-pod terminus is
Kind-provable while the cloud transport is **honestly marked scratch-GKE-deferred** (not faked — same
handling as the Phase-2 V-G scratch-GKE items).

**D2 — Per-tier watcher scoping with a fail-closed guard.** `k8s-event-watcher` today builds a
**cluster-wide** `SharedInformerFactory` (watcher.go:66) and the sidecar is injected with a hardcoded
`--owner=platform` (agent_manifests.go:762) for **every** tier — wrong owner attribution and, for the
namespace-scoped developer-team SA, a cluster-wide watch its `Role` cannot satisfy (crash-loop on
RBAC-deny). Fix: (1) plumb `--owner=<EffectiveTier(agent)>` from the controller (not hardcoded); (2) add a
distinct **`--scope-namespace`** informer flag → `informers.NewSharedInformerFactoryWithOptions(...,
informers.WithNamespace(ns))` for developer-team; platform/cluster-admin stay cluster-wide; (3) graft
`invariant-first`'s **fail-closed `validate()`**: a namespace-scoped tier (`--scope-namespace` set)
attempting a cluster-wide watch **exits non-zero at startup** rather than crash-looping against RBAC.
Watch stays **Events-only** (`core/v1.Event`) — the existing minimal, read-only surface. The existing
P3-K3 SAR read-only harness proves the watch identity is still read-only.

### Track B — OKF read + indirect coordination (06 §5)

**D4 — `read-knowledge`: a read-only OKF path that can never become a write path.** A new
`read-knowledge` skill in **all three** tiers reads `knowledge/` from the in-pod GitOps working copy.
Graft `invariant-first`'s **sparse, read-only checkout**: fetch only `knowledge/` (sparse-checkout +
`--depth=1`, read the fetched ref — never the deployable `clusters/` paths), so a read cannot materialize
a deployable tree or accumulate into a commit. Use a **contents:read-scoped** token (not the
`submit_suggestion` write token) and **hard-refuse** any push/commit subcommand in the read script.
Refactor `local-dev/okf-validate.py`'s `parse_frontmatter` into a **shared module** the read path imports,
so read and CI agree on the schema (no drift between "what validates" and "what an agent reads").

**D5 — Escalation round-trip via GitOps, parent re-derives its own scope.** A lower tier raises a
cross-tier request as an OKF `escalation` entry: a new **`raise-escalation`** skill writes
`knowledge/escalation/<slug>.md` (`type: escalation`, 06 §5) and opens a PR via `submit-suggestion`
(curate-as-code; a status lifecycle `open → ack → resolved` is itself edited by PR). The **parent** picks
it up on its next proactive sweep via `read-knowledge` in an **escalation-triage SOP** — and, grafting
`invariant-first`, **re-derives its own scope from its own CR** rather than trusting the entry's `to:`
field (hardens against a forged/misrouted escalation). There is **no** real-time cross-tier path — pickup
latency is bounded by the parent's cron cadence **plus** human merge of the escalation PR; this is
**correct** for the trust model (an escalation is "a request not yet a change") and is documented so
operators don't expect instant escalation. The no-direct-call property (invariant 3) is proven two ways:
a static assertion that the only cross-tier egress is loopback + the GitOps remote, and the per-tier
default-deny NetworkPolicy carrying **no child→parent destination rule**.

### Track C — Proactive SOPs + drift detection (04 §4/§5.1, 01 §7 SC4)

**D3 — Drift detection as a read-only diff → corrective PR, provable on Kind via `--dry-run`.** The
Platform Agent's **drift-detection SOP** (cron/scheduled-push) diffs **GitOps-desired vs. live (read-only
`get`)** state for RBAC / NetworkPolicy / version, and on divergence opens a **corrective PR** via
`submit-suggestion` — **unprompted, never a direct fix** (SC4). Two load-bearing details from the panel:
(1) **canonicalization + a curated ignore-set** (drop `managedFields`, `resourceVersion`, `status`,
server-defaulted fields) so the diff doesn't emit noisy false-positive PRs; (2) add a **`--dry-run`** flag
to `submit_suggestion.py` (all three tiers) that **halts after the local branch + commit**, before
`git push` / `gh pr create` — yielding an **observable corrective-PR artifact** (local branch + diff) that
Acc-e can assert on Kind **with no real GitHub**, plus a "the drifted live object is still present"
mutation guard (proving detect-and-propose, never fix). **Per-tier heartbeat SOPs** (D3 continued): wire
scoped audits for Cluster Admin (its cluster) and Developer Team (its namespace only) into each tier's
`cron/jobs.json` + a `governance/*_sop.md`, heartbeat demoted to backstop (04 §4).

---

## Ordering / dependency rule (critical)

**Track S is a hard pre-condition** — S1 (seam auth/loopback) and S2 (kind discriminator) land **before**
any new push source (D1/D2), because both widen exposure of the inject seam. After S, the tracks are
largely independent:

- **Track S order:** **P4-T1 (S1)** → **P4-T2 (S2)** (both edit all three `session_kv_server.py` copies;
  each leaves the tree green — S1 adds auth + loopback, S2 adds the `kind` branch).
- **Track A order:** **P4-T3 (D2)** (watcher scoping + fail-closed validate) → **P4-T4 (D1 cloud leg)**
  (eventingress Runnable + Kind synthetic-POST terminus). T3 gates T4's synthetic-alert delivery proof.
- **Track B order:** **P4-T5 (D4 read-knowledge)** first (introduces the shared frontmatter module +
  sparse read path the rest reuse) → **P4-T6 (D5 escalation round-trip)** (writes via submit-suggestion,
  parent picks up via read-knowledge). T6 gates on T5 + T7.
- **Track C order:** **P4-T7 (D3 `--dry-run`)** first (the hermetic write-path artifact both drift and
  escalation proofs depend on) → **P4-T8 (D3 drift SOP)** → **P4-T9 (per-tier heartbeat SOPs)**.
- **P4-T10 (knowledge seed)** adds a real `runbook` + `escalation/` dir so T5/T6 have live content;
  independent, can land early. **P4-T11 (verify + regress)** gates on all of T1–T10. **P4-T12 (docs +
  PR)** last.

> **STANDING BUILD-GREEN CONTRACT.** Every task must leave the tree **green** (compile + unit/golden)
> **and** must not regress the **03 §11 negative suite**. If a task cannot land without breaking green it
> is **split** until it can.
>
> **READ-ONLY-UNDER-TRIGGER invariant** (Phase-4 specific): a push trigger changes only _when_ an agent
> wakes, never _what_ it may do (04 §4). No task may give an event/cron/heartbeat path a mutation
> capability — every resulting change is a `submit-suggestion` PR. The watch identity stays the agent's
> pre-created read-only SA (03 §11 SAR harness must stay green).
>
> **NO-DIRECT-CALL invariant** (invariant 3): no task may add an agent→agent network path. Cross-tier
> flow is GitOps/OKF only; the escalation round-trip (T6) must remain provably indirect (loopback + Git
> remote egress only; no child→parent NetworkPolicy rule).

---

## Tasks

| ID     | Task                                                                                               | Track   | Implements                    | Files                                                                                                                                                                         | Acceptance signal                                                                                                                                                                                                                                                        | Status |
| ------ | -------------------------------------------------------------------------------------------------- | ------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ |
| P4-T1  | S1 — Harden the inject seam: bind `127.0.0.1` + enforce bearer/owner on `/sessions` + `/inject`    | S       | 03 §11 (invariant 5); 08 §2   | `deploy/shared/docker-entrypoint.sh`, `agents/{platform,cluster-admin,developer-team}/scripts/session_kv_server.py`                                                           | Uvicorn binds `127.0.0.1:8699`; `POST /sessions` + `/inject` with no/invalid bearer → 401/403; watcher's existing bearer+`X-Asserted-Caller` accepted; owner mismatch rejected; all 3 copies identical; existing inject path green                                       | ✅     |
| P4-T2  | S2 — `inject_message` `kind` discriminator branch (`k8s-event`/`alert`/`github`/`escalation`)      | S       | 04 §4; 06 §5                  | `agents/{platform,cluster-admin,developer-team}/scripts/session_kv_server.py`                                                                                                 | `{kind:alert}` / `{kind:github}` render correct (not coerced to CrashLoop); missing/unknown `kind` → 400; k8s-event path unchanged; unit test per kind across all 3 copies                                                                                               | ✅     |
| P4-T3  | D2 — Per-tier watcher scoping: `--owner=<tier>` + `--scope-namespace` + fail-closed `validate()`   | A       | 04 §4; 03 §4/§11; invariant 4 | `k8s-operator/cmd/k8s-event-watcher/{watcher,main}.go`, `k8s-operator/internal/controller/agent_manifests.go` (~:750-793), tests                                              | Controller renders `--owner=<EffectiveTier>` (not hardcoded platform) + `--scope-namespace` for dev-team; namespaced factory used; namespaced tier + cluster-wide watch → non-zero exit; platform/cluster-admin cluster-wide; go-test green                              | ✅     |
| P4-T4  | D1 — `eventingress` cloud-push Runnable (Pub/Sub alert + GitHub) + Kind synthetic-POST terminus    | A       | 04 §4; 05 §5                  | `k8s-operator/cmd/eventingress/**` (or agent sidecar), `agents/*/scripts/` glue, deploy manifest; Kind test script                                                            | Synthetic `{kind:alert}`/`{kind:github}` POST to `127.0.0.1:8699` spawns a session (Kind, hermetic); Pub/Sub subscribe-only (no publish); real transport marked **scratch-GKE-deferred, not faked**; egress netpol adds subscriber allow                                 | ✅     |
| P4-T5  | D4 — `read-knowledge` skill (all tiers): sparse read-only OKF checkout + shared frontmatter module | B       | 06 §5/§10; invariant 1        | `agents/{platform,cluster-admin,developer-team}/skills/read-knowledge/{SKILL.md,scripts/read_knowledge.py}`, `local-dev/okf-validate.py` (extract shared `parse_frontmatter`) | Retrieves a `runbook` by type/link from `knowledge/`; sparse checkout materializes **only** `knowledge/` (no `clusters/`); read script hard-refuses push/commit; contents:read token; okf-validate + read import the same parser                                         | ✅     |
| P4-T6  | D5 — Escalation round-trip: `raise-escalation` (lower tiers) + parent escalation-triage SOP        | B       | 06 §5; invariants 2,3,5       | `agents/{cluster-admin,developer-team}/skills/raise-escalation/**`, `agents/{platform,cluster-admin}/governance/escalation_triage_sop.md`, `cron/jobs.json`                   | Dev-team writes `knowledge/escalation/<slug>.md` via submit-suggestion (dry-run artifact); parent SOP reads it via read-knowledge, **re-derives its own scope** (ignores `to:`); no agent→agent call (loopback+Git egress only)                                          | ✅     |
| P4-T7  | D3 — `submit_suggestion.py --dry-run` (halt after local branch+commit, before push/PR) all tiers   | C       | 04 §5.1/§9                    | `agents/{platform,cluster-admin,developer-team}/skills/submit-suggestion/scripts/submit_suggestion.py` + tests                                                                | `--dry-run` stops before `push_branch`/`create_pull_request`; emits local branch + diff artifact; no `git push`, no `gh pr create`; exit 0; normal path unchanged; all 3 copies identical                                                                                | ✅     |
| P4-T8  | D3 — Platform drift-detection SOP: read-only diff (canonicalized) → corrective PR unprompted       | C       | 01 §7 SC4; 04 §5.1; 08 §7     | `agents/platform/governance/drift_detection_sop.md`, `agents/platform/cron/jobs.json`, helper script (canonicalize + ignore-set: managedFields/resourceVersion/status)        | Inject RBAC/netpol/version drift → SOP diffs desired-vs-live (read-only `get`), opens corrective PR (dry-run artifact); **drifted live object still present** (no direct fix); server-defaulted fields don't trip a false-positive PR                                    | ☐      |
| P4-T9  | D3 — Per-tier heartbeat SOPs: Cluster Admin (cluster) + Developer Team (namespace) scoped audits   | C       | 04 §4; 04 §9                  | `agents/cluster-admin/governance/*_sop.md` + `cron/jobs.json`, `agents/developer-team/governance/*_sop.md` + `cron/jobs.json`                                                 | Heartbeat SOPs run scoped audits (dev-team ns-only, cluster-admin cluster); anything to change → PR (no direct mutation); heartbeat documented as backstop after event/cron (04 §4)                                                                                      | ☐      |
| P4-T10 | Seed `knowledge/` with a real `runbook` + `escalation/` scaffold so read/escalation have content   | B       | 06 §5/§10                     | `examples/gitops-repo/knowledge/{index.md,runbook/*.md,escalation/.gitkeep}`                                                                                                  | `local-dev/okf-validate.py` passes (valid `type`, links resolve); a `runbook` entry exists for T5 to retrieve; `escalation/` dir exists for T6 to write into                                                                                                             | ☐      |
| P4-T11 | Phase 4 verification: `verify-phase4.sh` (Kind gate, Acc a–e) + go-test + regress                  | S+A+B+C | 07 §5; 04 §9; 06 §10; 03 §11  | `local-dev/kind/verify-phase4.sh` (new), reuse `verify-phase3.sh`/`negative-attenuation.sh`, watcher+router go-tests                                                          | `verify-phase4.sh` exit 0: scoped watch fires session (a), escalation round-trip indirect (b), runbook retrieved (c), scoped heartbeat (d), drift → corrective-PR artifact + object present (e); `go test ./...` green; **03 §11 + verify-phase3 + 05 §8 not regressed** | ☐      |
| P4-T12 | Docs (INSTALL Phase 4 section, LEDGER, memory) + open PR → main on fork; auto-merge                | all     | roadmap; AGENTS.md            | `INSTALL.md`, `docs/build/LEDGER.md`, memory                                                                                                                                  | PR opened on fork base `main`; all CI green + `mergeStateStatus: CLEAN`; no HALT; PR URL shared                                                                                                                                                                          | ☐      |

## Verification suites & Accept mapping

| Phase-4 Accept                                              | Proof                                                                                                                                             |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| (a) K8s watch fires a reaction **without** a heartbeat poll | P4-T3 scoped watcher + P4-T1/T2 seam: crash-looping pod → Event → inject spawns a session; dev-team watch is ns-scoped                            |
| (b) escalation **picked up by parent**, never a direct call | P4-T6 round-trip via GitOps/OKF (invariant 3, 03 §11): write→PR→parent read-knowledge; loopback+Git egress only, no child→parent netpol           |
| (c) agent **retrieves a runbook via OKF**                   | P4-T5 read-knowledge (06 §5/§10) + P4-T10 seed: sparse read-only checkout returns the runbook; read path can't push                               |
| (d) **per-tier heartbeats run scoped audits**               | P4-T9 (04 §4/§9): dev-team ns-only, cluster-admin cluster; change → PR, no direct-write audit events                                              |
| (e) **inject drift → corrective PR unprompted, never fix**  | P4-T8 drift SOP + P4-T7 `--dry-run` (SC4, 04 §5.1): diff → corrective-PR artifact; drifted live object still present                              |
| **Regression (halt on fail)**                               | 03 §11 negative suite (read-only under trigger; no-direct-call) + `verify-phase3.sh` + 05 §8 chaos re-run green; 08 §7 "controller mints no RBAC" |
| **Deferred, not faked**                                     | Cloud transport (alert Pub/Sub delivery, GitHub webhook HMAC) — **scratch-GKE**, Kind proves the in-pod terminus only (D1)                        |
