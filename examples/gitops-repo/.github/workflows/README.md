# .github/workflows/ — the actuation pipeline (customer's CI/CD)

kube-agents is **unopinionated** about CI/CD (06 §4). This directory holds the customer's pipeline
that, **on merge**, applies changed paths to the target — `kubectl apply` for K8s/KCC YAML,
`terraform apply` for HCL — using **least-privilege deploy credentials scoped per target**. Agents
hold no write credentials; the pipeline is the sole privileged writer (03 §4).

Reference workflows:

- `apply.yml` — **(Phase 1, present)** on merge to `main`, applies changed `clusters/**` / `fleet/**`
  artifacts to their target: `kubectl apply` for KCC/K8s YAML, `terraform apply` for HCL. Least
  privilege is per target via one **GitHub Environment per target** (each holds only its own
  Workload-Identity-Federation provider + deploy service account). Pin every `uses:` to a commit SHA
  in production and scope each deploy SA to its target only.
- `review-gate.yml` — _(added in a later phase)_ run the `review-security-k8s-*` suite via a headless
  harness runner on PRs touching guarded paths; block merge on unmitigated high/critical findings
  (Phase 5, 06 §7).

GitHub Actions is the reference; CircleCI/Jenkins/Argo/Flux/Atlantis are equally valid (06 §4). A
**second, equivalent** pipeline ships as [`../../.circleci/config.yml`](../../.circleci/config.yml)
(CircleCI) — same KCC/HCL dispatch, same per-target least-privilege creds, same merge-to-`main`
trigger — demonstrating actuation is genuinely pipeline-agnostic (Phase 7, 07 §2 Accept (b)). The two
are kept in parity by `local-dev/tests/circleci-parity.py`.
