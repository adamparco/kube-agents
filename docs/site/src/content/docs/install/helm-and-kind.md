---
title: Helm and Kind
description: A Helm chart for the Platform Agent and a Kind-based local install are proposed but not yet merged.
---

A Helm chart at `deploy/helm/platform-agent/` and a local development flow at `dev/setup-kind.sh` are proposed in [PR #353](https://github.com/gke-labs/kube-agents/pull/353) but not yet merged.

## Track progress

- [PR #353 — README overhaul + Helm + Kind](https://github.com/gke-labs/kube-agents/pull/353) — the umbrella change adding the chart and the Kind script.
- Watch [`deploy/`](https://github.com/gke-labs/kube-agents/tree/main/deploy) and [`dev/`](https://github.com/gke-labs/kube-agents/tree/main/dev) for the artifacts once they land.

This is upstream's proposal for a Kind-based install, and it is a different thing from this
repository's own inner loop, which stopped using Kind on 2026-07-26 and now runs on a remote GKE
cluster ([Operator development](/kube-agents/operator/development/)). `dev/` here is the
verification harness, not the `dev/setup-kind.sh` the PR adds.

## Install today

Until those merge, use:

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE + operator + agent.
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

This page will be rewritten when the chart and Kind flow are in `main`.
