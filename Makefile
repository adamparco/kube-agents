include tags.env

# The Artifact Registry region. This is not a free choice: it must match where the `kube-agents`
# repo actually exists, or every push here lands somewhere the cluster never pulls from. For
# adamparco-kage that is us-east4 -- `gcloud artifacts repositories describe kube-agents
# --location=us-central1` returns NOT_FOUND, which is what made `cloud-build-push` a no-op path.
LOCATION ?= us-east4
REPO ?= $(eval REPO := $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents)$(REPO)

BAD_SKILLS := $(wildcard agents/*/defaults/skills/*)

# The tag these targets build and push. Defaults to the working-tree commit, matching the convention
# `cloud-build-push` already used and the tag a live install ends up pinning (`tag: "src-abc1234"`).
#
# It is deliberately NOT :latest. These targets push into a real Artifact Registry that a real
# install then pulls from -- provision_12 wires CLUSTER_ADMIN_TAG/DEVELOPER_TEAM_TAG to AGENT_TAG --
# so :latest here meant a cluster could not say which build it was running, and `kubectl rollout
# undo` had nothing distinct to roll back to. Override with `make docker-push TAG=my-experiment`.
TAG ?= src-$(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

.PHONY: cloud-build-push default docker-build docker-build-agents docker-build-credential-proxy docker-push docker-push-agents docker-push-credential-proxy dev-rebuild-agent status prettier-check prettier-write validate

AGENTS := $(notdir $(patsubst %/,%,$(wildcard agents/*/)))


default: docker-build

# Docker builds
docker-build: docker-build-agents docker-build-credential-proxy
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target $* -t $(REPO)/$*-agent:$(TAG) -f deploy/docker/Dockerfile .

docker-build-credential-proxy:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target credential-proxy -t $(REPO)/credential-proxy:$(TAG) -f deploy/docker/Dockerfile .

# Docker pushes
docker-push: docker-push-agents docker-push-credential-proxy
docker-push-agents: $(foreach agent,$(AGENTS),docker-push-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-push-$(agent))
$(foreach agent,$(AGENTS),docker-push-$(agent)): docker-push-%: docker-build-%
	docker push $(REPO)/$*-agent:$(TAG)
	@echo "Pushed $(REPO)/$*-agent:$(TAG) — set AGENT_TAG=$(TAG) in k8s-operator/scripts/vars.sh"

docker-push-credential-proxy: docker-build-credential-proxy
	docker push $(REPO)/credential-proxy:$(TAG)

dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@$(MAKE) -C k8s-operator dev-rebuild-agent ARGS="$(ARGS)"

# Cloud Build produces amd64 images regardless of the host architecture, so this is the path to
# use from an arm64 machine (Apple silicon) — a local `docker build` there yields images the GKE
# nodes cannot run. It also pushes straight into the project's Artifact Registry.
#
# The other arm64 escape hatch is $PREBUILT_BINARY: both k8s-operator/Dockerfile and
# Dockerfile.router skip compilation and copy $TARGETPLATFORM/<binary> instead when it is set, which
# is how GoReleaser cross-compiles the Go images. It does NOT help the agent tiers — those are
# FROM nousresearch/hermes-agent, a whole userspace rather than one static binary, so Cloud Build is
# the only path for them. See docs/site/.../deploy/docker-images.md.
#
# Covers the operator and the router as well as the agent tiers: they are amd64-only for the same
# reason and were previously buildable on Apple silicon by no documented path at all.
#
# The seven builds run CONCURRENTLY, and that is the whole performance story. Cloud Build schedules
# each submission on its own worker, so they were never competing for anything -- the serial loop
# this replaced simply blocked the Mac on `Waiting for build to complete` seven times in a row.
# Measured 2026-07-26: ~5 min per image, ~35 min serial, and the slowest single image concurrently.
# Nothing here is shared between builds, so there is no ordering to preserve.
#
# Each job's output goes to its own log rather than the terminal: seven `gcloud` progress streams
# interleaved line-by-line is not readable, and the failing one has to be findable afterwards. The
# PID of every job is recorded and waited on INDIVIDUALLY -- a bare `wait` returns 0 and would
# report a green build for a push that never happened.
#
# SEVEN, and the seventh is why this is spelled out. `replay-proxy` is built and signed by
# docker-publish-ghcr.yml, is deployed on the live install as `standalone-replay`, and was in no
# `make` target at all -- so the one image with no local build path also had no remote one, while
# this target's own help text said "every first-party image". Its context is its own directory, not
# the repo root, which is why the spec list carries a context field rather than assuming one.
.PHONY: cloud-build-push
cloud-build-push: ## Build+push every first-party image via Cloud Build, concurrently (LOCATION/TAG overridable).
	@set -u; \
	. ./tags.env || exit 1; \
	TAG=$${TAG:-src-$$(git rev-parse --short HEAD)}; \
	REPO=$(LOCATION)-docker.pkg.dev/$$(gcloud config get core/project 2>/dev/null)/kube-agents; \
	logdir=$$(mktemp -d) || exit 1; \
	echo "Building into $$REPO with tag $$TAG"; \
	echo "Logs: $$logdir"; \
	jobs=''; \
	submit() { \
	  gcloud builds submit --config deploy/docker/cloudbuild.yaml \
	    --substitutions="_IMAGE_URI=$$REPO/$$1:$$TAG,_CACHE_URI=$$REPO/$$1:buildcache,$$2" \
	    >"$$logdir/$$1.log" 2>&1 & \
	  jobs="$$jobs $$!:$$1"; echo "  submitted $$1"; \
	}; \
	for target in $(AGENTS); do \
	  submit "$$target-agent" "_TARGET=$$target,_HERMES_AGENT_TAG=$$HERMES_AGENT_TAG"; \
	done; \
	submit credential-proxy "_TARGET=credential-proxy,_HERMES_AGENT_TAG=$$HERMES_AGENT_TAG"; \
	submit k8s-operator "_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile"; \
	submit kage-router  "_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile.router"; \
	submit replay-proxy "_CONTEXT=examples/inference-replay/replay-proxy,_DOCKERFILE=examples/inference-replay/replay-proxy/Dockerfile"; \
	echo "== $$(echo $$jobs | wc -w | tr -d ' ') builds running concurrently =="; \
	rc=0; \
	for j in $$jobs; do \
	  if wait "$${j%%:*}"; then echo "  ok    $${j##*:}"; \
	  else rc=1; echo "  FAIL  $${j##*:}  -- $$logdir/$${j##*:}.log"; fi; \
	done; \
	if [ "$$rc" -ne 0 ]; then \
	  echo "At least one image did NOT build. The tag $$TAG is INCOMPLETE in $$REPO --" >&2; \
	  echo "deploying from it would run a mix of this build and whatever was there before." >&2; \
	  exit 1; \
	fi; \
	echo "Done. In k8s-operator/scripts/vars.sh set:"; \
	echo "  AGENT_IMAGE=$$REPO/platform-agent   AGENT_TAG=$$TAG"; \
	echo "  OPERATOR_IMAGE=$$REPO/k8s-operator:$$TAG   ROUTER_IMAGE=$$REPO/kage-router:$$TAG"


status:
	git status

prettier-check:
	npx prettier --check "**/*.md" "**/*.yaml" "**/*.yml"

prettier-write:
	npx prettier --write "**/*.md" "**/*.yaml" "**/*.yml"

validate:
	@if [ -n "$(BAD_SKILLS)" ]; then \
		echo "Error: Skills should not be placed under agents/*/defaults/skills. Move them to agents/*/skills/"; \
		set -- $(BAD_SKILLS); \
		for file; do echo "  $$file"; done; \
		exit 1; \
	else \
		echo "Validation passed: No skills found in invalid paths."; \
	fi



