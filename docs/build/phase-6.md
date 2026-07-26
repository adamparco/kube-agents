# Phase 6 — Failure-isolation & resilience validation (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 6 — Failure-isolation & resilience
validation". **Goal:** prove the design's central resilience claim — **no cascade failure** (04 §6). This
phase lands the **load-bearing 05 §8 chaos suite**, which was honestly deferred (N-A) through Phases 4–5
and is now due. It adds **no new agent behaviour, persona, or write path**: it is a validation phase that
kills things (the hub, a Cluster Admin Agent, the controller) and asserts that running state and the other
tiers survive, and that the controller relaunches what it owns. Every chaos operation is **reversible and
Kind-guarded** — nothing here weakens an invariant.

**Phase acceptance (07 §2 "Accept") — decomposed a–c:**

- **(a)** **Hub down → the spoke keeps running its last-applied state.** Killing the hub (which hosts the
  shared inference + Minty services, 05 §3) leaves spoke **workloads and already-applied cluster state
  running** (Kubernetes keeps them up), and because actuation is the customer's CI/CD — independent of the
  kube-agents hub — an already-merged change can still deploy; spoke **agents pause** (no inference / no
  brokered token) and **resume on recovery** (04 §6 honest scoping).
- **(b)** **Cluster Admin down → its Developer Team Agents keep running.** Killing a Cluster Admin Agent
  leaves the Developer Team Agents in that cluster running (independent controller-reconciled pods); **new
  namespace provisioning pauses** (the proposer is gone) and **resumes on recovery** when the controller
  relaunches the Cluster Admin Agent.
- **(c)** **The controller relaunches agent pods.** A deleted agent **Deployment** is recreated by the
  kube-agents controller; a deleted agent **pod** is recreated by its Deployment (standard self-heal). No
  agent outage requires human intervention to recover the pod.

**Touched Verification suites:** **05 §8** — this phase **executes all four bullets** of it: (1) _controller
pod spec_ (SA / namespace / runtimeClassName / hardened securityContext) and (2) _placement_ (Platform in
the hub, each Cluster Admin in its cluster, each Developer Team in its namespace) are **regressed** from
their Phase-1/3/5 proofs (goldens + the A1 placement clause + the hardening VAP); (3) _failure isolation
(chaos)_ is the **net-new, load-bearing** work here (C1–C4 below); (4) _unopinionated actuation_ (nothing
requires a bundled GitOps engine) is asserted structurally. Also **04 §9** (the failure-isolation table,
04 §6) and the two load-bearing regression suites — **03 §11** (security negatives) and the prior phase
gates `verify-phase{2,3,4,5}.sh` — **must not regress**. **From this phase forward, 05 §8 is a live,
non-N-A load-bearing halt condition**, alongside 03 §11.

**Source of the breakdown:** a survey of the resilience surface — 04 §6's failure table (dev-team / cluster-admin
/ platform / controller down, each with Effect + Recovery) and its honest-scoping note that the hub is a
shared-fate dependency for _reasoning_ (not for _running state_); 05 §8's four verification bullets; the
controller's Deployment/pod construction (`agent_manifests.go`) and reconcile loop; the live dev Kind
cluster state (`kubeagents-controller-manager` 1/1 Running; the real `cluster-admin-cluster-a-gateway`
Agent CR + Deployment present but its pod **Pending** on the single-node cluster); and the existing
`verify-phase{2,3,4,5}.sh` gate pattern + destructive-test guard. The survey surfaced one decisive
implementation constraint that drives the fixture strategy: **the controller bakes prod-correct ~2Gi+
resource requests across a 4-container agent pod, so a real agent pod cannot schedule on the single-node
dev Kind** (it stays Pending). The load-bearing decisions **D1–D4** below resolve this before breakdown.

---

## Architecture decisions (load-bearing — resolved before breakdown)

**D1 — Chaos fixtures: real controller-behaviour + hardened stand-in pod-continuity.** The 05 §8 / 04 §6
properties are **lifecycle-level** — whether independent controller/Deployment-reconciled pods share fate,
and whether the controller (re)creates what it owns — **not** agent-reasoning-level. Because the controller
hardcodes ~2Gi+ requests across the 4-container agent pod (prod-correct; deliberately not made
CR-overridable just to fit a dev node), the real agent pod stays **Pending** on the single-node dev Kind.
Decision — split the fixtures so every claim is proven with the most faithful, actually-runnable evidence:

- **Reconcile-behaviour claims** — "no new reconciles while the controller is down", "reconcile resumes on
  restart", "the controller recreates a deleted agent Deployment" — use the **real**
  `agents.kubeagents.x-k8s.io` CR + the **real** controller. These are observed on the **Deployment
  object** (created / not-created / recreated), which is faithful **regardless of whether the pod
  schedules** — so a Pending pod does not weaken them. No proxy.
- **Pod-continuity / no-cascade claims** — "running pods continue while X is down", "a deleted pod is
  relaunched", "tier Y survives tier X's death" — need **actually-Running** pods. They use **lightweight
  stand-in Deployments** labeled `kube-agents/tier=<tier>` with the **full hardened securityContext**
  (`readOnlyRootFilesystem`, `runAsNonRoot`, drop-ALL, seccomp `RuntimeDefault`) so they are **admitted
  under the very same ceiling a real agent pod faces** (PSS `restricted` + the `kube-agents-agent-pod-hardening`
  VAP), running a tiny `registry.k8s.io/pause` image with minimal requests so they schedule. The property
  they prove — that independent Deployment-managed pods do not share fate — is a Kubernetes-level property
  faithfully represented by any independent Deployment-managed pod. Marked as a **proxy for the agent
  workload**, in a dedicated `kube-agents-chaos` namespace, torn down after.

**D2 — "Kill" = reversible, single-object, Kind-guarded.** Chaos operations are destructive: scale
`kubeagents-controller-manager`→0, delete a single agent Deployment, delete a single pod. Per the
destructive-test guard, `chaos-suite.sh` **anchors the context to `kind-*`** and halts otherwise. Every op
is **reversible** (scale back to 1; the controller/Deployment self-heals what was deleted) and
**single-object** — no `--all`, no `--force --grace-period=0` mass deletion, no namespace nukes beyond the
suite's own `kube-agents-chaos`. A cleanup trap restores the controller replica count and removes the chaos
namespace on any exit.

**D3 — Hub-down is proven at the "cluster-state-survives" layer on Kind; agent-reasoning-pause is honestly
deferred.** 04 §6's honest scoping is explicit: spoke autonomy under hub loss means **the cluster keeps
running its last-applied state**, **not** that spoke agents keep operating (inference + Minty are hub-hosted
shared-fate for _reasoning_). On single-cluster Kind with no real hub/inference, **C4 proves the
load-bearing, testable half** — an already-applied spoke workload and its running state **survive an
inference outage**, and the workload is **structurally decoupled** from the hub (no ownerRef / data-path
edge) — and **defers the literal "spoke agent pauses because it cannot reach real hub-hosted inference/Minty
over private networking"** to a two-cluster / scratch-GKE run. Recorded, **not faked** — mirroring the V-G
cloud + Calico-egress deferrals of earlier phases.

**D4 — Continuity is proven by polling, recovery by a bounded wait — never a single snapshot.** "Pod X
keeps running while Y is down" is asserted by capturing X's **pod UID + readiness** and **polling across the
entire disruption window** that the UID never changes and it never goes NotReady — adversarially
distinguishing genuine continuity from "it happened to be up when we looked once". "Z is relaunched /
reconciled" is proven by waiting for the **new** object to appear with a **bounded timeout that fails
loudly** (not hangs) if it does not — so a broken controller reads as FAIL, never as a silent pass.

---

## Ordering / dependency rule (critical)

1. **P6-T1 (scaffold: guard + helpers + fixtures) → C1–C4.** The `kind-*` guard, the poll/wait helpers
   (D4), the `kube-agents-chaos` namespace + cleanup trap (D2), and the stand-in/real-CR fixture builders
   (D1) must exist before any experiment runs.
2. **C1 → C2 → C3 → C4** each add a section to the single `chaos-suite.sh`. C2 (controller relaunch) reuses
   C1's controller-up precondition; C3/C4 use only stand-ins and are independent of C1/C2.
3. **`verify-phase6.sh` is last within the phase** — it runs `chaos-suite.sh` (05 §8 bullet 3 + bullet 4),
   asserts Accept (a–c), then runs **regression** (05 §8 bullets 1–2 via goldens/placement, 03 §11
   `negative-attenuation.sh`, `verify-phase{2,3,4,5}.sh`, `go test ./...`). The chaos ops **must leave the
   cluster in a state where every prior gate still passes** — the cleanup trap + controller/Deployment
   restore (D2) are what make the regression trustworthy.

---

## Tasks

| ID    | Task                                                                                                                                  | Implements (doc §)                                                                             | Files (primary)                                                                                                               | Acceptance signal                                                                                                                                                                                                                                                    | Status |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| P6-T1 | Chaos harness scaffold: `kind-*` guard, poll/wait helpers, `kube-agents-chaos` ns + cleanup trap, stand-in + real-CR fixture builders | 05 §8; 07 §5; invariants (guard)                                                               | `dev/verify/chaos-suite.sh`                                                                                                   | `bash -n` clean; guard **refuses** a non-`kind-*` context (exit 2); a no-experiment run sets up + tears down `kube-agents-chaos` idempotently; a `tier-labeled hardened stand-in` Deployment is **admitted** (VAP/PSS) and reaches Ready                             | ⬜     |
| P6-T2 | **C1 — controller down**: running pod continues; **no new reconciles**; **resume on restart**                                         | 05 §8 (chaos); 04 §6 (controller/cluster-admin rows); Accept (b)                               | `dev/verify/chaos-suite.sh`                                                                                                   | Scale controller→0: a Running stand-in pod's UID/readiness unchanged (D4); a **new valid Agent CR** gets **no Deployment** within the bounded wait; scale→1 (leader re-elected) → the CR **now reconciles** a Deployment; controller restored                        | ⬜     |
| P6-T3 | **C2 — controller relaunches agent pods**: deleted Deployment recreated; deleted pod recreated                                        | 05 §8 (chaos); 04 §6 (all rows "controller relaunches"); Accept (c)                            | `dev/verify/chaos-suite.sh`                                                                                                   | With controller up, deleting the **real** `cluster-admin-cluster-a-gateway` Deployment → controller **recreates** it (correct labels/ownerRef) within the bounded wait; deleting a running stand-in **pod** → its Deployment recreates the pod                       | ⬜     |
| P6-T4 | **C3 — cluster-admin down → dev-team survives (no cascade)** + cluster-admin relaunched                                               | 05 §8 (chaos "kill a Cluster Admin Agent"); 04 §6 (cluster-admin row); Accept (b)              | `dev/verify/chaos-suite.sh`                                                                                                   | With a `cluster-admin` + a `developer-team` stand-in both Running, deleting the cluster-admin pod leaves the dev-team pod **UID-stable + never NotReady** across the window (polled, D4); the cluster-admin pod is **relaunched** (recovery)                         | ⬜     |
| P6-T5 | **C4 — hub down → last-applied spoke state survives**; structural decoupling; no bundled GitOps engine                                | 05 §8 (chaos "kill the hub" + unopinionated actuation); 04 §6 (hub honest-scoping); Accept (a) | `dev/verify/chaos-suite.sh`                                                                                                   | Scaling a `hub-inference` stand-in→0 leaves a `spoke-workload` stand-in **Ready across the window**; the workload has **no ownerRef to the hub/agent** and selects nothing hub-side; **no Config Sync/Connector/Argo/Flux** required; agent-pause deferred-not-faked | ⬜     |
| P6-T6 | `verify-phase6.sh` consolidated gate + regression (05 §8 graduates to live load-bearing)                                              | 07 §5; 05 §8; 04 §9; 03 §11; 08 §7                                                             | `dev/verify/verify-phase6.sh` (new); reuse `chaos-suite.sh`, `negative-attenuation.sh`, `verify-phase{2,3,4,5}.sh`, `go test` | `verify-phase6.sh kind-kube-agents-dev` **exit 0**: C1–C4 + unopinionated-actuation pass (Accept a–c); 05 §8 bullets 1–2 regressed (pod spec + placement goldens); **03 §11 + verify-phase{2,3,4,5} + `go test ./...` not regressed**                                | ⬜     |
| P6-T7 | Docs (INSTALL Phase 6 section + ToC, LEDGER, memory) + open PR → `main` on fork; auto-merge                                           | roadmap; AGENTS.md                                                                             | `INSTALL.md`, `docs/build/LEDGER.md`, memory                                                                                  | PR opened on fork base `main`; CI green + only the benign Auto-Request-Review red; no HALT; PR URL shared; auto-merged once the gate passes; local `main` fast-forwarded                                                                                             | ⬜     |

---

## Verification suites & Accept mapping

| Accept | Proof (live Kind chaos, polled per D4)                                                                                                                                                                                                                                                 | Task(s)      |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| (a)    | **C4**: a `spoke-workload` stand-in stays Ready across a `hub-inference`→0 outage; structurally decoupled (no ownerRef/data-path to the hub); no bundled GitOps engine required. Agent-reasoning-pause deferred (D3).                                                                  | P6-T5        |
| (b)    | **C3**: a `developer-team` stand-in is UID-stable + never NotReady while the `cluster-admin` stand-in is killed; cluster-admin relaunched on recovery. **C1**: a new Agent CR gets no reconcile while the controller is down, and reconciles on restart (provisioning pauses/resumes). | P6-T4, P6-T2 |
| (c)    | **C2**: the controller recreates a deleted agent **Deployment**; a deleted agent **pod** is recreated by its Deployment.                                                                                                                                                               | P6-T3        |

**05 §8 full-section execution:** bullet (1) _controller pod spec_ + bullet (2) _placement_ are **regressed**
via `go test` goldens (SA/image/hardened SC) + the A1 placement clause (`verify-phase3.sh` P3-K1) + the
hardening VAP (`verify-phase5.sh` c); bullet (3) _failure-isolation chaos_ is **net-new** (C1–C4); bullet
(4) _unopinionated actuation_ is asserted in `chaos-suite.sh` (no Config Sync/Config Connector/Argo/Flux CRD
is a dependency; actuation is `apply.yml` in GitHub Actions).

**Regression (must stay green — halt on failure):** `negative-attenuation.sh` + `vap-agent-readonly`
(03 §11 read-only ceiling), `verify-phase2.sh` / `verify-phase3.sh` / `verify-phase4.sh` /
`verify-phase5.sh` (prior-phase Accept, incl. Calico egress + hardening VAP), `go test ./...` (08 §7
controller mints no RBAC; goldens). The chaos ops must be fully undone (D2 cleanup trap) before these run.

**Deferred-not-faked (recorded, not silently dropped):** real **spoke agent-reasoning-pause under real hub
loss** (needs a real hub + inference/Minty over private networking — two-cluster / scratch-GKE; D3); the
scratch-GKE **V-G** cloud checks still pending from Phase 2; and the **cross-object webhook, gVisor
execution sandbox, and per-request user down-scoping** remain deferred hardening (08 §5). None of these are
asserted green.
