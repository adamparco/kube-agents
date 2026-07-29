# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

`kube-agents` is a Kubernetes agentic harness: a Go operator that runs LLM agents as pods inside GKE, plus the agent blueprints (personas, skills, governance) those pods load. It has three layers that are easy to confuse:

1. **The product** — `k8s-operator/` (Go controller + `kage-router` + eventingress) and `agents/` (agent source of truth). Deployed to a real GKE cluster.
2. **The provisioning path** — `k8s-operator/scripts/provision_NN_*.sh` (13 ordered steps, each with a matching `teardown_NN_*.sh`), driven by `provision.sh` and configured by `vars.sh`.
3. **The build harness** — `.claude/harness/` + `docs/build/` + `docs/design/`. An autonomous, spec-driven build loop that _constructs_ layers 1 and 2. See "The build harness" below; if you are asked to advance the build, you must operate through it, not around it.

`AGENTS.md` holds PR hygiene rules that apply to every change.

## Common commands

```bash
# Operator (run from repo root; k8s-operator has its own Makefile)
make -C k8s-operator build          # manifests + generate + fmt + vet + build
make -C k8s-operator test           # envtest-backed controller tests
cd k8s-operator && go test ./...    # unit tests without envtest
cd k8s-operator && go test ./internal/controller -run TestAgentReconcile   # single test

# Python check suites
python3 -m unittest discover dev                     # all dev/test_*.py
python3 -m unittest dev.test_detect_drift            # one module
python3 -m unittest discover -s scripts/review-gate  # review-gate scorer

# Repo lint / format
make validate                       # no skills under agents/*/defaults/skills
npx prettier --write <files>        # md/yaml/json — CI checks the WHOLE branch diff vs origin/main
actionlint -color                   # only when .github/workflows changed

# Docs site
cd docs/site && npm ci && npm run build
```

### The L0 chain is the real pre-commit check

`dev/L0-CHAIN.txt` is the single definition site for every check that needs no cluster and no network. It is read by `.github/workflows/l0-checks.yml`, by the harness regress step, and by `dev/tests/invariants-gate.py`. Run it before pushing:

```bash
while IFS= read -r c; do case "$c" in ''|\#*) continue ;; esac; eval "$c" || echo "FAIL: $c"; done < dev/L0-CHAIN.txt
```

Add a new L0 check by adding one line to that file — never by adding it to the workflow. `dev/L2-CHAIN.txt` is the cluster-requiring companion; every line carries its own `--context` explicitly and none of them run in PR CI.

### Images: this repo refuses host-arch builds

Every deploy target is amd64. On Apple silicon a local `docker build` produces images that push and pull cleanly and then die with `exec format error`, so `make docker-build*` and `make -C k8s-operator docker-build` **exit 2** on a non-amd64 host. Use `make cloud-build-push` (builds all seven first-party images concurrently via Cloud Build). `ALLOW_HOST_ARCH_BUILD=1` overrides but gives you an image for your machine, not for a node.

Redeploy through `bash dev/cluster/reload-images.sh {operator|router|agents|all} <context>` — it builds, pushes, reads the digest back out of Artifact Registry, and **deploys by digest**. Never `kubectl rollout restart`: a same-tag restart cannot prove which build is running.

### Addressing a cluster

`k8s-operator/Makefile` targets that touch a cluster go through `$(KUBECTL)`, set from `KUBE_CONTEXT=`. Passing `KUBECTL=` is a hard error (it used to be silently ignored). An unset `KUBE_CONTEXT` is only accepted when the ambient context is anchored `gke-scratch-*`.

Clusters in play:

- `gke-scratch-kube-agents-dev` (project `adamparco-kage`, `us-east4-a`) — the only inner-loop / destructive-test target. Built by `dev/cluster/up.sh`; `pause.sh`/`resume.sh` scale node pools to 0 and back for between-campaign idling.
- `platform-agent-host` (`us-east4`) — the live install. Verification only. **Never a destructive-test target.**

Destructive scripts guard with an anchored shell `case` on `gke-scratch-*` whose `*)` arm exits non-zero. Keep it anchored; a substring glob puts the live cluster one `*` away.

## Architecture notes

- **Operator** (`k8s-operator/`): `api/v1alpha1/agent_types.go` defines the `Agent` CRD; `internal/controller/` reconciles it into agent pods (`pod_launcher.go`) via rendered manifests (`agent_manifests.go`, golden-tested). `internal/webhook/` enforces admission; `internal/router/` is `kage-router`, the read-only ChatOps front door; `internal/eventingress/` ingests cluster events. Config lives in `config/` (kubebuilder layout).
- **Runtime-authoritative config**: the operator renders a ConfigMap that _shadows_ the image-baked `agents/<tier>/config.yaml`. Assert against the rendered ConfigMap, not the file in the image.
- **Agents** (`agents/{platform,cluster-admin,developer-team}/`): each has `SOUL.md` (persona), `config.yaml`, `AGENTS.md`, and `skills/` — skills must live in `agents/*/skills/`, never `agents/*/defaults/skills/` (`make validate` enforces this). Each skill directory needs a `SKILL.md`.
- **Security gate**: `scripts/review-gate/score_findings.py --waivers security-review-waivers.yaml` is the authoritative merge decision — any unmitigated high/critical exits 1.

## The build harness

If the task is "advance the build", read `.claude/harness/README.md` and `PROTOCOL.md`, then work through the skills: `/harness-run` (one bounded unit), `/harness-verify` (checks by stable ID), `/harness-milestone` (close a phase), `/harness-improve` (mechanize lessons). Never mix them.

- **`.claude/harness/binding.md` is the lookup table for every project-specific value** — spec paths, gates, build/test commands, cluster targets, preconditions P1–P10, branching, merge rules, thresholds. If a value there is stale, fix it there; never work around it in a skill or a check.
- **All state lives in `docs/build/LEDGER.md`** — read first, written last, every session. Phase task breakdowns are `docs/build/phase-<N>.md`.
- **`docs/build/BACKLOG.md` is the human inbox** — the one harness file a person may append to at any time, including mid-unit, without racing a write. It changes nothing until the next ORIENT, which drains it and schedules each item. Never resolve an item from it in the middle of a unit.
- **`docs/design/01`–`09`** is the source of truth; `09-verification-and-validation.md` is the conformance spec (check IDs `V-<SUITE>-<nnn>`, never reused or renumbered).
- **`.claude/harness/LESSONS.md`** records every mistake this repo has already paid for and the mechanized check that now catches it. Read the ones tagged for the area you are touching — most of the surprising rules in this file trace to one.
- Verdicts are `pass` / `fail` / `deferred`. A check that could not run its property is `deferred` with a named blocker, never `pass`.

## Git and PR mechanics

- `origin` = `https://github.com/adamparco/kube-agents` (the fork) — every branch, PR, and merge lives here. `upstream` = `gke-labs/kube-agents`, never a push target or diff base. Local `main` tracks **upstream**, so always diff against `origin/main` after `git fetch origin`.
- Prettier must run over the full `origin/main...HEAD` changed set, not just this session's edits — CI checks the whole branch.
- Conventional Commits; stage by explicit path. Never `git add -A` or `git add .`.
- `gh pr create --base main --head <branch>` with a body from `.github/PULL_REQUEST_TEMPLATE.md`. Never `--fill`. Merge with `gh pr merge <n> --squash --delete-branch`; never `--admin` or `--no-verify`.
- There are no known-benign red checks. A red required check blocks.
- Always surface the PR URL when a PR is created or updated.
