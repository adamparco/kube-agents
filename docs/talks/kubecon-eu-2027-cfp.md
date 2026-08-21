# KubeCon + CloudNativeCon Europe 2027 — CFP submission draft

Barcelona, 15–18 March 2027. CFP closes **11 October 2026, 23:59 CEST**. Submit at
<https://sessionize.com/kubecon-cloudnativecon-europe-2027/>. Session Presentation format is
**30 minutes**, 1–2 speakers.

Character counts below are measured, not estimated. CNCF asks for the Session Description in
**third person, full sentences**; the Benefits field is where first person belongs.

---

## Session Title

> Replacing the Presentation Layer: Upgrade Sequencing and Capacity Chasing

Alternatives, best first:

| Title                                                                    | Chars |
| ------------------------------------------------------------------------ | ----- |
| Replacing the Presentation Layer: The Operations That Never Had an Owner | 72    |
| Kubernetes Already Has Every Knob. Nobody Turns Them.                    | 53    |
| After the Dashboard: Agents, Upgrade Sequencing, and Scarce GPUs         | 64    |
| The Fleet Decisions Nobody Ever Encoded a Controller For                 | 56    |

## Session Description

> Kubernetes has a knob for almost everything: maintenance exclusions, PodDisruptionBudgets, surge
> upgrades, ComputeClass priorities, DRA fallbacks. Each executes a ladder somebody authored before
> the situation existed, and nothing re-authors it when a reservation expires or a new machine
> family ships. kubectl and dashboards are the presentation layer over that gap, and the operations
> that live there have never had an owner.
>
> This talk reports seventeen days of putting open source agents in that seat, on two of them:
> workload-aware upgrade sequencing, and capacity chasing across reservations, quota and
> obtainability history. It covers the 32 single-file pull requests the agents opened, the 2 a human
> merged and the 10 a human closed, and the morning a run reported a clean fleet it had never
> inspected.
>
> Platform engineers and SREs will leave with a test for which decisions belong in a controller and
> which do not. No LLM background required.

## Benefits to the Ecosystem

> The debate in cloud native is what agents can do. The more useful question is where a
> reconciliation loop stops. The boundary is authoring, not executing. Karpenter derives its own
> candidate set and is the honest counterexample; enumerated ladders like ComputeClass priorities
> and ordered failover do not, and the accelerator decisions that hurt most sit behind enumerated
> ones.
>
> Read-only investigation is settled now that k8sgpt and HolmesGPT are CNCF Sandbox projects. The
> open question is what must be true before an agent proposes a change to production, and this talk
> answers with an acceptance rate rather than a demo: of 32 remediation pull requests, humans merged
> 2 and closed 10, while 13 auto-merged in test repositories. It shows a fabricated all-clear and
> the rule that fixed it, which any agent framework can adopt: every check publishes the command
> that ran it, or the run is assumed blind.
>
> The harness is Apache-2.0, validated on GKE; the talk says which half of the work travels.

## CNCF and Open Source Projects

> **CNCF projects:** Kubernetes — the harness ships a controller-runtime operator reconciling a
> PlatformAgent CRD in the `kubeagents.x-k8s.io` group. Envoy — the credential proxy is an Envoy
> sidecar that holds every credential outside the agent's container. Argo CD and Flux — the agent's
> only write path is a commit to a GitOps repository, reconciled by whichever of these is installed.
> Helm and Prometheus. Karpenter, Kueue, JobSet and the descheduler are discussed as prior art: the
> talk's argument is about where their reach ends, and Karpenter is presented as the counterexample
> to our own thesis.
>
> **Other open source projects:** kubernetes-sigs/devops-bench, which the evaluation harness
> consumes as a library; gVisor, the RuntimeClass the agent pod runs under; LiteLLM for model
> routing; Kustomize; and Model Context Protocol for tool integration. The project under discussion,
> kube-agents, is Apache-2.0 and is not a CNCF project.

## Supporting fields

| Field                | Answer                                                                                                                                                                                                                                                                                      |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Track                | Platform Engineering: Infrastructure & Delivery — the subject is a GitOps write path, an operator and a CRD, and multi-cluster fleet lifecycle. Second choice: Agentic AI, but it will be the most oversubscribed track at the event.                                                       |
| Session format       | Session Presentation, 30 minutes                                                                                                                                                                                                                                                            |
| Experience level     | Intermediate. Assumes node-pool upgrades, PodDisruptionBudgets, eviction, autoscaling and GitOps delivery. Assumes no LLM or agent-framework background.                                                                                                                                    |
| Case study?          | **No.** Seventeen days across mostly dev and test fleets is a pilot, and CNCF's definition of a case study is an organisation's real-world implementation and how well it worked.                                                                                                           |
| Previously presented | No. The framing was published by the same team in [a May 2026 Google Open Source Blog post](https://opensource.googleblog.com/2026/05/disrupting-the-presentation-layer-using-autonomous-workflows.html); CNCF's rule covers talks presented at LF events within a year, which this is not. |

## Measured character counts

| Field       | Chars | Limit                                      |
| ----------- | ----- | ------------------------------------------ |
| Title       | 73    | 75 in an unofficial template; unconfirmed  |
| Description | 950   | 1000 (stated by the submitter)             |
| Benefits    | 997   | Unpublished for EU 2027; 1000–1500 by year |

## Talk outline, 30 minutes

- **0:00–2:00 — The gap.** One slide: the inventory of knobs Kubernetes already has for upgrade
  safety and capacity fallback. Every one of them executes a ladder a human wrote in advance.
- **2:00–5:00 — Four minutes arguing against this talk.** Maintenance exclusions and windows, surge
  and blue-green, PDBs honoured by the eviction path, Karpenter disruption budgets and
  `do-not-disrupt`, cluster-autoscaler `safe-to-evict`, ComputeClass ordered priorities, DRA
  prioritized lists, Kueue TAS and ProvisioningRequest, SkyPilot ordered failover. Then kill our own
  best example: if your training loop knows its checkpoint cadence, that is a ten-line annotation
  and you should use it, not us. Slide reads "Do not build this."
- **5:00–8:00 — The residue.** What survives that table. Open-world enumeration: every mechanism
  optimises inside a candidate set someone authored, and an enumerated ladder does not regenerate
  when a quota grant or a new machine family changes the right answer. Constraint-based selectors
  such as Karpenter's requirements and DRA's CEL expressions **do** regenerate — that distinction is
  the content, stated out loud, with Karpenter on the side that refutes us. Trades with no common
  unit. Per-signal integration cost. The N=1 tax: a controller is amortisation machinery, and these
  decisions happen once.
- **8:00–10:00 — The reframe.** The presentation layer is where the decisions nobody encoded get
  made by a human. Move that work and the layer loses its reason to exist. State the bound
  immediately: this replaces the human interface, never human approval.
- **10:00–15:00 — Case 1, drain safety.** The audit that answers which workloads block a node drain.
  A `maxUnavailable: 0` PDB on a single replica does not degrade a workload — it stalls node-pool
  upgrades, auto-repair and scale-down indefinitely. Show a real remediation pull request: one file,
  the selector reproduced verbatim, the literal `kubectl` command that found it in the body. Then
  the refusal beside it, where the agent could not reproduce the selector and filed a finding
  instead of a fix.
- **15:00–19:00 — Case 2, upgrade sequencing.** The inversion: a fixed-reservation GPU pool is
  safest at `maxSurge=0`, because surge assumes capacity you can reclaim. Nobody re-derives that per
  pool. Advisory today — the agent produces the plan, a human runs it — and say so on the slide.
- **19:00–21:00 — Case 3, capacity chasing.** Ninety seconds on the join: obtainability history,
  reservations, project quota, and a fallback the agent refuses to recommend because it cannot prove
  the shape is purchasable. Then the portable finding: of those four sources, how many have any
  Kubernetes API representation at all.
- **21:00–24:00 — The boundary.** What the agent cannot do, and what enforces it: no writes to
  cluster state beyond leader-election leases in its own namespace, cannot read Secrets, RBAC
  enforced; credentials held in an Envoy sidecar and never in the agent container; hour-scoped
  GitHub App installation tokens. The harness opens a pull request and stops — whether it merges is
  your branch protection, and in our test repositories auto-merge was on, which is where 13 of our
  15 merges came from.
- **24:00–27:00 — The failure.** A run asked to do five audit streams at once issued zero `kubectl`
  commands, hand-wrote five empty findings documents, and published a fleet-wide all-clear. The fix
  is structural, not a better prompt: every check publishes the command that ran it, a coverage gap
  can never report resolved, and silence has to be earned by a script rather than decided by a
  model.
- **27:00–30:00 — Numbers and what we would tell you to do.** 32 pull requests across four GitOps
  repositories in seventeen days; half of them under 17 lines; humans merged 2 and closed 10. The
  test to take home: if the decision recurs and you can enumerate its inputs, write the controller.
  If it happens once and the inputs are open-world, that is the work nobody was ever going to
  encode.

## Notes for the submitter

**Hostile questions to expect.**

1. _"Karpenter already regenerates its candidate set."_ Correct, and it is on the slide as the
   counterexample. The distinction is enumerated versus derived, and the mechanisms holding today's
   accelerator decisions are the enumerated ones.
2. _"This is a policy engine with extra steps."_ The baseline is not the controller, it is the human
   doing what no controller was configured to do. The deliverable is a reviewable single-file diff.
3. _"Just write the controller."_ A controller is amortisation machinery and these operations happen
   once.
4. _"Non-deterministic automation with cluster credentials."_ No write path to the cluster exists;
   the output is a pull request.
5. _"Kueue/SIG-Scheduling already does this."_ SIG-Scheduling declares cross-domain trades out of
   scope deliberately. This is not a gap in Kueue.

**Do before submitting.**

- Re-derive every number the week you submit; the corpus is still growing.
- Verify against primary sources: DRA prioritized-list stability, the k8sgpt and HolmesGPT sandbox
  statuses, and the one-hour PDB eviction honour.
- Capture a real screenshot for the Case 1 pull request, authored by one of the harness's bot
  identities, and confirm the narration matches the author shown.
- Confirm the live Sessionize character limits — only the 1000 for the description is known.
- Decide who speaks. CNCF will not accept three or more speakers who all identify as men, and
  panelists must represent more than one company.
