# Phase 5 — Security gate & hardening (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 5 — Security gate & hardening".
**Goal:** make the security model **continuously enforced** rather than set-once. Four deltas land: (1)
the **review-gate CI** (06 §7) — the agent-driven `review-security-k8s-*` skills run on every PR (and a
heartbeat re-run) via a **headless harness runner**, emit findings with a **severity**, and a **hermetic
scoring step** turns "any unmitigated high/critical" into a **merge block**; (2) a **per-tier egress
allowlist** for all three tiers (platform is net-new) plus a **real enforcement proof** (NetworkPolicy is
actually enforced, not just structurally valid); (3) the **hardened pod-security context on every agent
pod** made continuously enforced (PSS `restricted` + a focused VAP) and completed
(`readOnlyRootFilesystem`); (4) **end-to-end attribution** — the authenticated requester + trace ID are
carried from the router through the session into the mutation PR, so every change ties back to who asked.
Nothing here relaxes an invariant: agents stay read-only, the only write path is a reviewed PR, and the
new controls only **enforce** the ceiling that was previously merely configured.

**Phase acceptance (07 §2 "Accept") — decomposed a–d:**

- **(a)** A **PR with an unmitigated high finding is BLOCKED** — the review-gate runs the
  `review-security-k8s-main` + `review-security-k8s-agents-main` suites on the PR, and an **unmitigated
  high/critical** finding fails the gate (medium/low advisory); a finding covered by a valid waiver, or a
  clean PR, passes (06 §7).
- **(b)** **Egress outside the allowlist is DENIED** — each tier's default-deny + pure-allowlist
  NetworkPolicy is present (platform, cluster-admin, developer-team), and on a NetworkPolicy-**enforcing**
  CNI an off-allowlist destination is actually blocked while an on-allowlist one is allowed (03/05).
- **(c)** **Every agent pod runs under the hardened security context** — the operator renders the full
  hardened context (incl. `readOnlyRootFilesystem`) **and** it is continuously enforced (PSS
  `enforce: restricted` + a securityContext VAP), so a hand-edited pod that drops it is rejected at
  admission (08 §5, 08 §7).
- **(d)** **Every mutation is attributable** — the authenticated requester + trace/session ID flow router
  → inject seam → session → PR, stamped as durable trailers on the mutation PR, correlating the chat turn
  to the change it produced (06 §8, DoD item 6).

**Touched Verification suites:** **06 §10** (review-gate JSON schema now carries `severity`; scoring
decision reproducible), **03 §11** (the load-bearing negatives — the read-only RBAC ceiling
[`vap-agent-readonly` + `negative-attenuation.sh`] **must not regress**, and the new pod-hardening VAP
must compose with it, not bypass it; egress **enforcement** negative), **08 §7** (pre-created identity;
controller mints no RBAC; golden manifests updated for the hardened context — regress), plus **05 §8**
(chaos — regress, N-A until Phase 6). **Load-bearing subset active this phase: 03 §11** — the read-only
ceiling and the no-direct-call invariant must survive the new enforcement layer, and **egress enforcement**
graduates from structural-only to actually-denied.

**Source of the breakdown:** a survey of the Phase-5 security surface (the `.agents/skills/review-security-k8s-*`
suite + its JSON schema; `.github/workflows/`; 06 §7's normative review-gate contract; the operator's
`agent_manifests.go` securityContext path + golden fixtures; the per-tier egress netpols +
`verify-phase3` P3-K6; the router `audit.go` + `submit_suggestion.py` attribution path; the
`vap-agent-readonly` VAP + `negative-attenuation.sh` negatives; and the existing `verify-phase{2,3,4}.sh`
gate pattern) mapped every Accept bullet to its **existing coverage vs net-new gap**. The four gaps that
drive the tasks: (a) the skills emit `{message,file,line}` with **no `severity`** and there is **no CI
job, scoring step, or waiver mechanism**; (b) there is **no platform-tier egress policy** and enforcement
is only ever proven ad-hoc off-Kind; (c) the hardened context is set **imperatively** with **no admission
enforcement** and **`readOnlyRootFilesystem` missing** on agent containers (though present on the
operator's own); (d) the router logs `Sender` per chat turn but **nothing ties that requester to the PR**
the turn eventually produces. The load-bearing decisions **R-A/B/C, E-A, H-A/B, T-A** below resolve these
before breakdown.

---

## Architecture decisions (load-bearing — resolved before breakdown)

### Track R — Review-gate CI (06 §7)

**R-A — Split the gate into an agent-driven detector and a hermetic enforcer.** 06 §7 mandates the review
runs via a **headless harness invocation** (the skills are agent-driven), that **CI is authoritative and
runs outside the agent**, and that a **scoring step turns the JSON into the blocking decision**. Decision:
the review-gate workflow has two clean halves. (1) A **detector** step invokes the two orchestrator skills
**headlessly** (`claude -p` / agent runner) with **read-only** repo + cluster creds, writing the
aggregated JSON finding array to a file. This step needs a model credential (`ANTHROPIC_API_KEY` secret)
and live-ish context, so it **gracefully skips on fork PRs / when the secret is absent** (exactly like the
existing `auto_request_review` fork limitation) — never a silent pass, always an explicit skip note. (2) A
**scorer** step (pure Python, **no agent**, always runs) reads the JSON + waivers and **decides the
block**. This split makes the _enforcement_ fully **hermetic and unit-testable** (fixture JSON → exit
code) while keeping the _detection_ agent-driven per spec; the scorer is the authoritative gate, the
detector only supplies evidence. An in-agent pre-check stays advisory-only (06 §7).

**R-B — Severity taxonomy on the finding schema.** The block rule ("unmitigated **high/critical** blocks;
medium/low advisory", 06 §7) requires a severity the skills don't emit today. Decision: extend the finding
schema from `{message,file,line}` to `{message,file,line,severity}` with `severity ∈
{critical,high,medium,low}`, and add a short severity rubric to both orchestrators
(`review-security-k8s-main`, `review-security-k8s-agents-main`) so every specialist tags findings
consistently (e.g. read-only-ceiling break / privilege-escalation / metadata-endpoint reachable →
critical/high; missing `readOnlyRootFilesystem` → medium; style → low). Backward-compatible: a finding
with no `severity` is treated as **high** by the scorer (fail-safe, never silently downgraded).

**R-C — Waiver = "mitigation", with a fingerprint + expiry.** "Unmitigated" (06 §7) needs a concrete
mechanism. Decision: a repo-root `security-review-waivers.yaml` — a list of
`{fingerprint, justification, approved_by, expires}` entries. A finding's **fingerprint** =
`sha256(agent + "\n" + file + "\n" + normalize(message))[:16]` (message normalized: lowercased,
whitespace-collapsed, line/line-number tokens stripped) so it is stable across re-runs but specific to the
finding. The scorer drops a high/critical finding **iff** a non-expired waiver matches its fingerprint;
an expired or absent waiver ⇒ unmitigated ⇒ block. Waivers are themselves a reviewed change (they live in
the repo and go through the same PR), preserving attributability of the mitigation decision.

### Track E — Per-tier egress allowlists + enforcement (03, 05)

**E-A — Add the missing platform policy; prove enforcement on an enforcing CNI, defer L7.** Decision: add
the **platform-tier** egress NetworkPolicy (net-new) mirroring the cluster-admin shape (default-deny +
pure allowlist, `podSelector: kube-agents/tier: platform`, `REPLACE_WITH_*` CIDR placeholders, **no
`0.0.0.0/0`**), so all three tiers have a policy. For Accept (b)'s **"denied"** — which kindnet cannot
prove (verify-phase3 P3-K6 is explicitly _structural only_) — stand up a **Calico-backed** test
(`local-dev/kind/kind-calico.yaml` + `local-dev/tests/egress-enforcement.sh`) that applies a tier's
default-deny + allowlist, then asserts an **off-allowlist** destination is actually **blocked** and an
**on-allowlist** one is **allowed** — adversarially distinguishing a real deny from a DNS/setup error.
Where a Calico cluster isn't reachable in the inner loop, the same script runs on **scratch GKE** and is
marked **deferred-not-faked** (never asserted-green without the enforcing CNI). The **hostname-precise L7
egress proxy** the netpol headers call out remains **deferred hardening** (L3/L4 policy can't express
hostnames) — documented, not silently dropped.

### Track H — Hardened pod-security context enforced (08 §5, 08 §7)

**H-A — Complete the context: `readOnlyRootFilesystem` + writable `emptyDir`.** Decision: set
`readOnlyRootFilesystem: true` on **every** agent container in `agent_manifests.go` (bringing agent pods
up to the operator's own bar — the operator's manager/router/github workloads already set it), and mount a
writable `emptyDir` at each path the runtime must write (`/tmp` and the agent work dir), so a read-only
root doesn't break the runtime. Regenerate the golden fixtures (`testdata/**/expected/agent.yaml`) and
extend the `pod_launcher` parity assertion to include it.

**H-B — Continuous enforcement via PSS `restricted` + a focused hardening VAP (compose, don't collide).**
Decision: label the agent namespaces `pod-security.kubernetes.io/enforce: restricted` (+ `warn`/`audit`)
— the pods already satisfy restricted-PSS's core (runAsNonRoot, seccomp RuntimeDefault, drop-ALL,
no-privesc). Because restricted-PSS does **not** cover `readOnlyRootFilesystem`, add a small
`vap-agent-pod-hardening.yaml` `ValidatingAdmissionPolicy` (bound `Deny`) that asserts
`readOnlyRootFilesystem: true` on pods labeled `kube-agents/tier`. This is **orthogonal** to
`vap-agent-readonly` (that VAP governs **RBAC** objects; this one governs **pods**) so they compose and
neither weakens the other — a hand-edited Deployment that drops the hardened context is rejected at
admission, giving Accept (c) real teeth beyond the imperative render.

### Track T — End-to-end attribution (06 §8, DoD 6)

**T-A — Carry requester + trace ID router → inject → session → PR.** Decision: reuse the Phase-4 inject
seam. The router (which already stamps `Sender`/`ThreadID` in `audit.go`) includes a **`trace_id`** and
**`requester`** in the inject payload; `inject_message` (the S2 discriminator seam) records them into the
session KV; `submit_suggestion.py` (all three tiers) reads them and stamps durable **`Requested-by:
<requester>`** + **`Trace-Id: <id>`** trailers into the PR body/commit message. This closes the
requester → PR link end-to-end (06 §8: "authenticated requester carried through … the merge/approver
identity and PR URL are the durable attribution"), so every mutation is provably attributable to the chat
turn that asked for it — without adding any write path.

---

## Ordering / dependency rule (critical)

1. **R-B (severity) → R-C (waiver) → R-A scorer → R-A workflow.** Severity and the waiver format define
   the JSON the scorer consumes; the scorer is the authoritative gate; the workflow wires the detector to
   the scorer. Build and unit-test the **scorer hermetically first** (it is the Accept-(a) proof), then
   wire CI.
2. **E-A netpol (shape) → E-A enforcement test.** The platform policy must exist before enforcement can be
   proven across all tiers.
3. **H-A (render `readOnlyRootFilesystem` + goldens) → H-B (enforce).** Enforce only what the operator
   already renders, or admission would reject the operator's own pods.
4. **T-A** threads through the existing inject seam (P4 S1/S2) and `submit_suggestion.py` (P4 D3) — depends
   on nothing new here.
5. **verify-phase5.sh** is last within the phase; **regression** (03 §11 negatives + verify-phase{2,3,4})
   runs before the gate/checkpoint. The pod-hardening VAP (H-B) must be proven to **not regress**
   `vap-agent-readonly` / `negative-attenuation.sh`.

---

## Tasks

| ID     | Task                                                                                                  | Track   | Implements (doc §)           | Files (primary)                                                                                                                                                          | Acceptance signal                                                                                                                                                                                                                        | Status |
| ------ | ----------------------------------------------------------------------------------------------------- | ------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| P5-T1  | R-B — Add `severity` to the review-skill finding schema + severity rubric on both orchestrators       | R       | 06 §7; 06 §10                | `.agents/skills/review-security-k8s-main/SKILL.md`, `.agents/skills/review-security-k8s-agents-main/SKILL.md`, specialist `SKILL.md`s                                    | Schema is `{message,file,line,severity}`; `severity ∈ {critical,high,medium,low}` with a rubric; orchestrators instruct specialists to tag; skill-review meta-lint still clean                                                           | ☐      |
| P5-T2  | R-C — Waiver/mitigation format + fingerprint scheme                                                   | R       | 06 §7                        | `security-review-waivers.yaml` (root), `docs/build/phase-5/review-gate-waivers.md` (format doc)                                                                          | Waiver entry `{fingerprint,justification,approved_by,expires}`; fingerprint = `sha256(agent\nfile\nnormalize(message))[:16]`; expiry semantics documented                                                                                | ☐      |
| P5-T3  | R-A(scorer) — Hermetic scoring step: JSON + waivers → block decision, unit-tested                     | R       | 06 §7; 07 §5                 | `scripts/review-gate/score_findings.py`, `scripts/review-gate/test_score_findings.py`                                                                                    | Unmitigated critical/high → exit 1; valid waiver / clean → exit 0; expired waiver → block; missing `severity` treated as high; fixtures cover each; dependency-free `unittest` passes offline                                            | ☐      |
| P5-T4  | R-A(workflow) — `review-gate.yml`: 06 §7 path-glob + heartbeat triggers; headless detector → scorer   | R       | 06 §7                        | `.github/workflows/review-gate.yml`, `scripts/review-gate/run-detector.sh` (headless runner glue)                                                                        | Triggers on `**/{provisioning,agents,namespaces,policy}/**` + agent config/`SOUL.md` + a heartbeat `schedule`; detector step skips gracefully w/o `ANTHROPIC_API_KEY`; scorer always runs & gates; actionlint-clean                      | ☐      |
| P5-T5  | E-A — Platform-tier egress NetworkPolicy (net-new); all three tiers present                           | E       | 03; 05                       | `examples/gitops-repo/clusters/cluster-a/agents/netpol-platform-egress.yaml`                                                                                             | `NetworkPolicy` selects `kube-agents/tier: platform`, `policyTypes:[Egress]`, pure allowlist (DNS + hub + Google APIs + GitHub + MCP `REPLACE_WITH_*`), **no `0.0.0.0/0`**; server-dry-run valid after CIDR substitution                 | ☐      |
| P5-T6  | E-A — Egress **enforcement** proof on Calico (off-allowlist DENIED / on-allowlist ALLOWED)            | E       | 03 §11; 05                   | `local-dev/kind/kind-calico.yaml`, `local-dev/tests/egress-enforcement.sh`                                                                                               | On a Calico cluster: default-deny + allowlist applied → off-allowlist egress **blocked**, on-allowlist **allowed**; real-deny-vs-setup-error distinguished; no Calico ⇒ scratch-GKE, **deferred-not-faked**; L7 proxy noted deferred     | ☐      |
| P5-T7  | H-A — `readOnlyRootFilesystem: true` on all agent containers + writable `emptyDir`; regen goldens     | H       | 08 §5; 08 §7                 | `k8s-operator/internal/controller/agent_manifests.go`, `k8s-operator/internal/testing/testdata/**/expected/agent.yaml`, `pod_launcher_test.go`                           | Every agent container SC adds `readOnlyRootFilesystem: true`; `/tmp` + workdir `emptyDir` mounts render; goldens updated; `go test ./internal/...` green; parity test asserts it                                                         | ☐      |
| P5-T8  | H-B — Enforce the context: PSS `enforce: restricted` label + `vap-agent-pod-hardening` VAP            | H       | 08 §5; 03 §11                | agent namespace manifests (`examples/gitops-repo/.../namespaces/*`, `kubeagents-system`), `examples/gitops-repo/policy/vap-agent-pod-hardening.yaml`                     | Agent namespaces carry `pod-security.kubernetes.io/enforce: restricted`; VAP (bound `Deny`) rejects a `kube-agents/tier` pod without `readOnlyRootFilesystem`; **does not** conflict with `vap-agent-readonly` (composes)                | ☐      |
| P5-T9  | T-A — Attribution: carry `trace_id`+`requester` router→inject→session→PR trailers (all tiers)         | T       | 06 §8; DoD 6                 | `k8s-operator/internal/router/*` (payload), `agents/*/scripts/session_kv_server.py` (`inject_message`), `agents/*/skills/submit-suggestion/scripts/submit_suggestion.py` | Router puts `trace_id`+`requester` in inject payload; `inject_message` records them; `submit_suggestion` stamps `Requested-by:`/`Trace-Id:` PR trailers (all 3 copies identical); unit test asserts trailers; router audit ties `Sender` | ☐      |
| P5-T10 | Phase 5 verification: `verify-phase5.sh` (Kind gate, Acc a–d) + Calico egress regress + regress prior | R+E+H+T | 07 §5; 06 §10; 03 §11; 08 §7 | `local-dev/kind/verify-phase5.sh` (new), reuse `egress-enforcement.sh`/`negative-attenuation.sh`/`verify-phase{2,3,4}.sh`, scorer + go tests                             | `verify-phase5.sh` exit 0: scorer blocks unmitigated-high (a), all-tier netpol shape + Calico deny (b), goldens+VAP dry-run reject un-hardened pod (c), PR trailer + audit tie (d); **03 §11 + verify-phase{2,3,4} not regressed**       | ☐      |
| P5-T11 | Docs (INSTALL Phase 5 section, LEDGER, memory) + open PR → main on fork; auto-merge                   | all     | roadmap; AGENTS.md           | `INSTALL.md`, `docs/build/LEDGER.md`, memory                                                                                                                             | PR opened on fork base `main`; CI green + `mergeStateStatus: CLEAN`; no HALT; PR URL shared; auto-merged once gate passes                                                                                                                | ☐      |

---

## Verification suites & Accept mapping

| Accept | Proof (hermetic first)                                                                                                                                                          | Task(s)        |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| (a)    | `score_findings.py` fixtures: an unmitigated `high`/`critical` finding → **exit 1** (blocked); a clean array and a validly-waived high → exit 0; expired waiver → blocked       | P5-T1,T2,T3,T4 |
| (b)    | All three tiers carry a pure-allowlist egress netpol (no `0.0.0.0/0`), server-dry-run valid; **Calico** enforcement test: off-allowlist DENIED, on-allowlist ALLOWED            | P5-T5,T6       |
| (c)    | Golden `agent.yaml` shows `readOnlyRootFilesystem: true` on every container; `vap-agent-pod-hardening` dry-run **rejects** an un-hardened `kube-agents/tier` pod; PSS label set | P5-T7,T8       |
| (d)    | `submit_suggestion` unit test asserts `Requested-by:`/`Trace-Id:` PR trailers from an injected `trace_id`+`requester`; router audit records `Sender`↔`trace_id`                 | P5-T9          |

**Regression (must stay green — halt on failure):** `negative-attenuation.sh` + `vap-agent-readonly`
(03 §11 read-only ceiling), `verify-phase2.sh` / `verify-phase3.sh` / `verify-phase4.sh` (prior-phase
Accept), `go test ./...` (08 §7 controller mints no RBAC; goldens). **05 §8 chaos** is **Phase 6 — N-A**
here (marked honestly, not skipped).

**Deferred-not-faked (recorded, not silently dropped):** the **hostname-precise L7 egress proxy** (E-A;
L3/L4 policy can't express hostnames); a **live headless detector run** needs the `ANTHROPIC_API_KEY`
secret + live creds (skips on fork PRs, like `auto_request_review`); Calico egress enforcement runs on
**scratch GKE** where no Calico inner-loop cluster is reachable; and the **cross-object webhook, gVisor
execution sandbox, and per-request user down-scoping** remain deferred hardening (08 §5).
