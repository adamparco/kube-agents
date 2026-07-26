---
title: Docker images
description: The images shipped from this repo and how their tags are managed.
sidebar:
  order: 2
---

Images published by this repo, plus the base Hermes image (pulled from Docker Hub).

## Published images

Seven images, all published to `ghcr.io/gke-labs/kube-agents/` and mirrored to GAR at
`us-docker.pkg.dev/kube-agents-prow/kube-agents/`. Every one is signed with
[cosign](https://docs.sigstore.dev/cosign/signing/overview/) against its digest.

| Image                  | Built from                            | Published by                      |
| ---------------------- | ------------------------------------- | --------------------------------- |
| `platform-agent`       | `deploy/docker/Dockerfile` (platform) | `docker-publish-ghcr.yml`         |
| `cluster-admin-agent`  | `deploy/docker/Dockerfile`            | `docker-publish-ghcr.yml`         |
| `developer-team-agent` | `deploy/docker/Dockerfile`            | `docker-publish-ghcr.yml`         |
| `credential-proxy`     | `deploy/docker/Dockerfile`            | `docker-publish-ghcr.yml`         |
| `replay-proxy`         | `examples/inference-replay/`          | `docker-publish-ghcr.yml`         |
| `k8s-operator`         | `k8s-operator/Dockerfile`             | `docker-publish-k8s-operator.yml` |
| `kage-router`          | `k8s-operator/Dockerfile.router`      | `docker-publish-k8s-operator.yml` |

All three agent tiers derive `FROM agent-base`, which layers the tooling an agent needs to inspect
and remediate clusters onto `nousresearch/hermes-agent`: `google-cloud-cli` plus
`google-cloud-cli-gke-gcloud-auth-plugin`, `kubectl`, and `curl`, `jq`, `dnsutils`,
`iputils-ping`, `patch`, `git`. Each tier then adds its own persona and skill set, which is why the
tiers are separate images rather than one image with a flag.

## Tags

**Every published tag is immutable. There is no `:latest`.**

| Trigger            | Tag produced            |
| ------------------ | ----------------------- |
| push to `main`     | `:${git-sha}`           |
| push of a `v*` tag | `:v0.1.0` (the git tag) |

The version comes from `KAGE_IMAGE_VERSION` in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env),
which is the single source of truth: every manifest, kustomization and provisioning default that
pins a first-party image pins that value. Cutting a release is therefore two steps — bump
`KAGE_IMAGE_VERSION`, then push the matching git tag. The publish workflows **refuse** a tag build
whose git tag disagrees with `tags.env`, so the version a user pins and the version actually
published cannot drift apart.

This is enforced offline, on every PR, by `dev/test_image_provenance.py` (check **V-CMP-002**):
it joins the set of `(image, tag)` pairs referenced anywhere in the tree against the set the
workflows produce, and fails on any reference to an image no workflow builds or a tag no workflow
publishes. It exists because both halves of that join were broken at once — three images had
Dockerfile targets that nothing built, and every manifest in the tree pinned a `:v0.1.0` that was
published for no image at all. Neither is visible from inside the file that is wrong.

The one deliberately mutable tag is `:buildcache`, a BuildKit inline-cache manifest used by the
Cloud Build path. It is not deployable, and V-CMP-002 fails if anything references it.

For production, prefer a digest over any tag — `platform-agent@sha256:...`. Kustomize supports this
directly with `digest:` in place of `newTag:`.

## Base image pin

The Hermes base image is pinned by digest in `tags.env` as `HERMES_AGENT_TAG`, and reaches the
build as a build arg:

```dockerfile
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base
```

Bumping Hermes = updating that one line and rebuilding.

## Building on Apple silicon

A local `docker build` on an arm64 Mac produces arm64 images, which GKE nodes cannot run. There are
two ways out, and which one applies depends on the image:

**The Go images** (`k8s-operator`, `kage-router`) accept a `PREBUILT_BINARY` build arg. When it is
set, the Dockerfile skips compilation entirely and copies `${TARGETPLATFORM}/${PREBUILT_BINARY}`
instead — the layout GoReleaser produces. That is how the release pipeline cross-compiles them, and
it works locally with any cross-compiled binary placed at that path:

```bash
GOOS=linux GOARCH=amd64 go build -o linux/amd64/manager cmd/main.go
docker build --build-arg PREBUILT_BINARY=manager -f k8s-operator/Dockerfile k8s-operator
```

**The agent tiers** have no such hatch, and cannot: they are `FROM nousresearch/hermes-agent`, a
whole userspace rather than a single static binary, so there is nothing to cross-compile and
substitute. For those, Cloud Build is the only path — it builds amd64 regardless of your host:

```bash
make cloud-build-push            # every first-party image, tagged src-<sha>
make cloud-build-push TAG=my-experiment
```

It prints the `AGENT_IMAGE` / `AGENT_TAG` / `OPERATOR_IMAGE` / `ROUTER_IMAGE` values to set in
`k8s-operator/scripts/vars.sh` afterwards.

## Local builds

For development iteration, `make dev-rebuild-agent` (from `k8s-operator/`) is the fast path — it
builds and pushes to a dev Artifact Registry repo and restarts the Deployment. See
[Development](/kube-agents/operator/development/#fast-agent-iteration-dev-only).

`make docker-build` / `make docker-push` from the repo root build into your own Artifact Registry,
tagged `src-<sha>` from the working-tree commit. Override with `make docker-push TAG=...`.

## CI

Every image above is built — but not pushed — on every PR by
[`.github/workflows/docker-build.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-build.yml).
The set there and the set published from `main` are asserted equal by V-CMP-002: an image whose
first build happens after merge has no PR that could have caught it breaking, which is how three of
these images once reached a release phase with targets nothing had ever built.
