---
title: Development
description: Build, test, and iterate on the operator locally.
sidebar:
  order: 2
---

The operator is a standard Kubebuilder project. Standard workflow — `make generate`, `make manifests`, `make test`, `make docker-build`, `make deploy`.

Everything below runs from `k8s-operator/`.

## Prerequisites

- Go 1.24+.
- `docker` (or `podman`) for `make docker-build`.
- `gcloud`, authenticated with a project set — the inner-loop cluster and its images live in GCP, and `dev/cluster/*` builds on Cloud Build rather than a local daemon.
- `kubectl` pointed at a target cluster for `make install` / `make deploy`.
- `make` — the entire workflow is Makefile-driven.

## Build

```bash
make generate      # regenerate deepcopy code
make manifests     # regenerate CRDs, ClusterRoles, WebhookConfiguration
make build         # build the manager binary
```

Generated CRDs land in `config/crd/bases/`; RBAC in `config/rbac/`; webhook config in `config/webhook/`.

## Test

```bash
make test          # unit + envtest against a locally-fetched envtest binary
```

The envtest binaries are downloaded to `bin/` on first run (`make setup-envtest`).

## Run locally (against a real cluster)

```bash
export KUBE_CONTEXT=gke-scratch-kube-agents-dev   # which cluster; see below
make install       # install CRDs into $KUBE_CONTEXT
make run           # run the manager binary out-of-cluster, against the target cluster
```

Kill the process with Ctrl-C. `make uninstall` removes the CRDs.

Every target under `##@ Deployment` passes `--context` explicitly, taken from `KUBE_CONTEXT`. Leave it unset and the target reads your ambient context but **refuses** anything that is not `gke-scratch-*`, printing the command that would name it deliberately. Deploying to a real cluster is fine — you just have to say which one. `KUBECTL="kubectl --context …"` is rejected: it looked like it worked for a long time and was silently discarded.

## Deploy the manager into a cluster

```bash
make docker-build IMG=<your-registry>/kube-agents-operator:dev
make docker-push  IMG=<your-registry>/kube-agents-operator:dev
make deploy        IMG=<your-registry>/kube-agents-operator:dev
```

`make undeploy` removes it. `make deploy` builds nothing — it runs `kustomize set image` and `kubectl apply` — so the tag you name is the binary you get. For the inner-loop cluster, use the helper below, which builds first.

## The inner-loop cluster

The inner loop runs on a remote GKE cluster: `kube-agents-dev` in `us-east4-a`, two `e2-standard-4` nodes, **Dataplane V2**, **Workload Identity**. None of the three is incidental. A dataplane that takes a `NetworkPolicy`, stores it and enforces none of it turns every green from a network check into a statement about the API server's willingness to persist YAML — Dataplane V2 enforces, and GKE cannot enable it on an existing cluster, so it is a create-time choice or nothing. Two nodes because RWO volumes exclude per node: a one-node cluster does not fail the multi-node claims, it quietly turns them into deferrals. Workload Identity because it costs nothing at create time and removes "there is no metadata server" as a reason the identity checks cannot run — it does not discharge them; nobody has bound a GSA to an agent KSA here.

```bash
dev/cluster/up.sh        # create or refresh; 5-8 minutes on a first create
dev/cluster/pause.sh     # nodes -> 0 between sessions; etcd is in the control plane, so nothing is lost
dev/cluster/resume.sh    # nodes back to 2, ~2 minutes
dev/cluster/down.sh      # delete the cluster
```

`up.sh` is idempotent and installs the whole thing — cert-manager, the operator built from your working tree and deployed by digest, the read-only VAP, the three tier agent images — so re-running it is the supported way to pick up a source change.

It also renames the context `gcloud` generates (`gke_<project>_<zone>_<cluster>`) to **`gke-scratch-kube-agents-dev`**. That prefix is a security control, not a label: every destructive script under `dev/` guards on an anchored `case … gke-scratch-*)`, and `dev/tests/invariants-gate.py` asserts the guards stay anchored. The rename is what makes this cluster addressable by the suite at all — and what keeps the live install, which nothing renames, out of its reach.

### Getting your code onto it

```bash
dev/cluster/reload-images.sh operator gke-scratch-kube-agents-dev   # controller + webhook
dev/cluster/reload-images.sh router   gke-scratch-kube-agents-dev   # kage-router (Dockerfile.router)
dev/cluster/reload-images.sh agents   gke-scratch-kube-agents-dev   # the three tier agent images
dev/cluster/reload-images.sh all      gke-scratch-kube-agents-dev
```

The helper builds on Cloud Build, pushes to Artifact Registry, reads the digest back out of the registry, and points the workload at `…@sha256:…`. Three things follow from that:

- **Cloud Build is not the slow path here, it is the correct one.** The nodes are amd64 and the developer host is arm64. A local build produces images the cluster cannot execute, and the failure surfaces minutes later as `CrashLoopBackOff` with `exec format error`, in a different component.
- **The digest is read back from the registry, not taken from the builder.** A successful `gcloud builds submit` says a build ran; it does not say what that tag now resolves to in the registry the kubelet pulls from.
- **No `rollout restart`, and no `imagePullPolicy` to get right.** A changed digest changes the Deployment spec, which _is_ a rollout; an unchanged digest is genuinely the same image, and restarting it would prove nothing.

### Verifying against it

Every phase left a re-runnable gate under `dev/verify/`, and each takes the context as its argument:

```bash
dev/verify/verify-phase8.sh gke-scratch-kube-agents-dev
```

## Fast agent iteration (dev only)

For local Platform Agent development you don't want to run the full provisioner every time. `make dev-rebuild-agent` shells out to `k8s-operator/scripts/dev/dev_rebuild_agent.sh`:

```bash
make dev-rebuild-agent ARGS="platform"
```

This builds the agent workspace image, pushes to Artifact Registry, and restarts the Deployment. First run creates a dev Artifact Registry repo; clean it up later with `make gcp-teardown-dev-artifact-registry`.

## Integrations (Kustomize)

Integrations have dedicated deploy/undeploy targets:

```bash
make deploy-litellm             # LiteLLM Gateway
make deploy-inference-replay    # inference-replay proxy
make deploy-github              # Minty (GitHub token minter)
```

Each has a matching `undeploy-*` target. These are the same kustomize bases the provisioner uses.

## Formatting

```bash
make prettier-check    # verify Markdown/YAML/JSON formatting
make prettier-write    # apply formatting
```

Prettier is enforced in CI ([`.github/workflows/prettier.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/prettier.yml)).

## CI

Relevant workflows:

- [`k8s-operator-test.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/k8s-operator-test.yml) — runs `make test`.
- [`docker-publish-k8s-operator.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-k8s-operator.yml) — publishes the manager image.
- [`e2e-gchat-test.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/e2e-gchat-test.yml) — end-to-end Google Chat test.
